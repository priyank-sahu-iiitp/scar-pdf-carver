#!/usr/bin/env python3
from optparse import OptionParser
import re
import os
import sys
import time
import bisect
import pickle
import PDF
import verify
from Carver import Reassembler

SECTOR_SIZE = 512

parser = OptionParser(usage="""%prog """)
parser.add_option('-i', '--index', default=None, help='Index file to operate on')
parser.add_option('-c', '--create', default=False, action="store_true", help='Create a new index file')
parser.add_option('-m', '--maps', default=False, action="store_true", help='Carve the index file by creating initial map files')
parser.add_option('-p', '--print', default=False, action="store_true", help='Print the index hits')
parser.add_option('-P', '--progress', default=False, action="store_true", help='Emit progress lines for the CLI')
parser.add_option('-X', '--no-xrefstream', default=False, action="store_true",
                  help='Disable C1 (PDF 1.5+ compressed xref-stream) decoding, for ablation studies')

(options, args) = parser.parse_args()

if options.no_xrefstream:
    PDF.DISABLE_XREF_STREAM = True

if not options.index:
    print("Need an index file to operate on.")
    sys.exit(1)

# Regex patterns for locating PDF structural markers
regexs = {
    'OBJECTS':    rb'(\d+) (\d+) obj',
    'XREFS':      rb'([\r\n ])xref([\r\n])',
    'STARTXREF':  rb'startxref([\r\n])',
    'PDFHEADER':  rb'%PDF-\d',
    'EOF':        rb'%%EOF',
    'XREFSTREAM': rb'/XRef',
}

cregexs = {}
hits = {}

# Keyword-to-category mapping for Aho-Corasick single-pass scan
_AC_KEYWORDS = {
    'obj': 'OBJECTS', 'xref': 'XREFS', 'startxref': 'STARTXREF',
    '%PDF-': 'PDFHEADER', '%%EOF': 'EOF', '/XRef': 'XREFSTREAM',
}
# Trailing bytes each regex extends past its keyword end
_KW_TRAIL = {'OBJECTS': 0, 'XREFS': 1, 'STARTXREF': 1,
             'PDFHEADER': 1, 'EOF': 0, 'XREFSTREAM': 0}


# Stage 1: scan disk image and build keyword index using Aho-Corasick
def build_index():
    import ahocorasick

    for k, v in regexs.items():
        cregexs[k] = re.compile(v)

    automaton = ahocorasick.Automaton()
    for kw in _AC_KEYWORDS:
        automaton.add_word(kw, kw)
    automaton.make_automaton()

    BLOCK_SIZE = 1 << 22
    OVERLAP = 64
    NEIGH_BACK = 48
    NEIGH_FWD = 12

    for category in set(_AC_KEYWORDS.values()):
        hits[category] = []

    t0 = time.perf_counter()
    total = os.path.getsize(args[0])
    base = 0
    tail = b''
    with open(args[0], 'rb') as fd:
        while True:
            chunk = fd.read(BLOCK_SIZE)
            if not chunk:
                break
            if options.progress:
                print("[PROGRESS] %d %d" % (base, total), flush=True)
            window = tail + chunk
            win_base = base - len(tail)
            text = window.decode('latin-1')
            for end_idx, kw in automaton.iter(text):
                category = _AC_KEYWORDS[kw]
                ns = max(0, end_idx - NEIGH_BACK)
                ne = min(len(window), end_idx + 1 + NEIGH_FWD)
                neigh = window[ns:ne]
                trail = _KW_TRAIL[category]
                for m in cregexs[category].finditer(neigh):
                    if ns + m.end() - trail == end_idx + 1:
                        hits[category].append(win_base + ns + m.start())
            base += len(chunk)
            tail = window[-OVERLAP:]
    if options.progress:
        print("[PROGRESS] %d %d" % (total, total), flush=True)
    elapsed = time.perf_counter() - t0

    for k in hits:
        hits[k] = sorted(set(hits[k]))

    with open(options.index, 'wb') as fd:
        pickle.dump(hits, fd)

    print("[E2] index built in %.3fs  (OBJECTS=%d XREFS=%d STARTXREF=%d)"
          % (elapsed, len(hits['OBJECTS']), len(hits['XREFS']),
             len(hits['STARTXREF'])))


# Write a .map file linking PDF logical offsets to disk image offsets
def save_map(filename, pdf, image_fd, hits):
    description = '\n#'.join(pdf.__str__().splitlines())

    obj_hits = hits.get('OBJECTS', [])  # already sorted by build_index

    # V3: find PDF physical start from the nearest %PDF- header before this xref
    try:
        xref_disk_pos = int(str(filename).split('-')[0])
    except (ValueError, IndexError):
        xref_disk_pos = None

    pdf_start = None
    if xref_disk_pos is not None:
        pre = [h for h in hits.get('PDFHEADER', []) if h <= xref_disk_pos]
        if pre:
            pdf_start = max(pre)

    def _match_direct(id_, offset, generation):
        """V3: locate object at pdf_start + offset (CONT scenario, ±16 bytes)."""
        target = pdf_start + offset
        lo = bisect.bisect_left(obj_hits, target - 16)
        hi = bisect.bisect_right(obj_hits, target + 16)
        for pos in obj_hits[lo:hi]:
            image_fd.seek(pos)
            line = image_fd.readline()
            exp = ("%d %d" % (id_, generation)).encode('latin-1')
            if line.startswith(exp):
                return (offset, pos, id_)
        return None

    def _match_sector(id_, offset, generation):
        """Fallback: original sector-alignment search (FRAG scenario)."""
        possibles = [x for x in obj_hits if x % 512 == offset % 512]
        found = []
        for pos in possibles:
            image_fd.seek(pos)
            line = image_fd.readline()
            exp = ("%d %d" % (id_, generation)).encode('latin-1')
            if line.startswith(exp):
                found.append((offset, pos, id_))
        return found

    n_type_n = sum(1 for _, _, _, t in pdf.xref if t == 'n')

    # Try V3 direct matching first
    direct_entries = []
    if pdf_start is not None:
        for id_, offset, generation, typ in pdf.xref:
            if typ != 'n':
                continue
            result = _match_direct(id_, offset, generation)
            if result:
                direct_entries.append(result)

    # Use direct entries if they cover ≥50% of type-n objects (CONT scenario).
    # Then supplement with sector alignment for any objects the direct pass missed
    # (e.g. incremental-update objects whose offsets are relative to a different
    # PDF base than the main body — direct matching fails for them but sector
    # alignment still finds them via mod-512 alignment).
    if pdf_start is not None and n_type_n > 0 and len(direct_entries) >= max(2, n_type_n * 0.5):
        entries = list(direct_entries)
        print("[V3] direct-match: %d/%d objects located for xref at %s"
              % (len(entries), n_type_n, filename))
        # Supplement: sector-align any object not already found by direct match
        direct_ids = {e[2] for e in direct_entries}
        for id_, offset, generation, typ in pdf.xref:
            if typ != 'n' or id_ in direct_ids:
                continue
            entries.extend(_match_sector(id_, offset, generation))
    else:
        # Fallback: sector alignment (FRAG scenario or sparse CONT)
        entries = []
        for id_, offset, generation, typ in pdf.xref:
            if typ != 'n':
                continue
            entries.extend(_match_sector(id_, offset, generation))

    # Upper bound: first %%EOF after the last mapped object
    bound = None
    if entries:
        max_img = max(e[1] for e in entries)
        later_eofs = [e for e in hits.get('EOF', []) if e > max_img]
        if later_eofs:
            bound = min(later_eofs) + len(b'%%EOF') + 64

    with open("%s.map" % filename, 'w') as fd:
        fd.write("#" + description + "\n")
        if bound is not None:
            fd.write("# BOUND %d\n" % bound)
        for (offset, possibility, id_) in entries:
            fd.write("%s %s Object_%s\n" % (offset, possibility, id_))


# Fallback: segment documents by %PDF- / %%EOF when xref is unavailable
def segment_documents(hits):
    headers = sorted(hits.get('PDFHEADER', []))
    eofs = sorted(hits.get('EOF', []))
    objs = sorted(hits.get('OBJECTS', []))
    docs = []
    for idx, h in enumerate(headers):
        h_next = headers[idx + 1] if idx + 1 < len(headers) else None
        lo = bisect.bisect_right(eofs, h)
        hi = bisect.bisect_left(eofs, h_next) if h_next is not None else len(eofs)
        if lo >= hi:
            continue
        doc_end = eofs[hi - 1] + len(b'%%EOF')
        n_obj = (bisect.bisect_right(objs, doc_end) -
                 bisect.bisect_left(objs, h))
        if n_obj == 0:
            continue
        docs.append((h, doc_end, n_obj))
    return docs


# Direct byte-range carve for a contiguous document (no sector mapping needed)
def carve_document_direct(image_fd, filename, doc_id, doc_start, doc_end):
    image_fd.seek(doc_start)
    data = image_fd.read(doc_end - doc_start)
    out = "%s.doc%d.objscan.pdf" % (filename, doc_id)
    with open(out, 'wb') as fd:
        fd.write(data)
    return out, len(data)


# V3 Improvement 2: build a synthetic xref from raw OBJECTS hits when the
# xref table/stream is missing or overwritten (e.g. deleted files on FAT/NTFS)
def _build_synthetic_xref(image_fd, pdf_start, pdf_end, obj_hits):
    lo = bisect.bisect_left(obj_hits, pdf_start)
    hi = bisect.bisect_right(obj_hits, pdf_end)
    synth = PDF.PDFFile()
    for pos in obj_hits[lo:hi]:
        image_fd.seek(pos)
        line = image_fd.readline(80)
        m = re.match(rb'(\d+)\s+(\d+)\s+obj', line)
        if not m:
            continue
        obj_num  = int(m.group(1))
        gen_num  = int(m.group(2))
        logical  = pos - pdf_start
        synth.xref.append([obj_num, logical, gen_num, 'n'])
    if len(synth.xref) < 4:
        return None
    nums = [e[0] for e in synth.xref]
    synth.xref_range = [min(nums), max(nums) + 1]
    return synth


def print_xrefs():
    with open(options.index, 'rb') as fd:
        hits_local = pickle.load(fd)

    image_fd = open(args[0], 'rb')
    total_xrefs = {}

    # Parse classic xref tables
    for xref in hits_local.get('XREFS', []):
        image_fd.seek(xref, 0)
        p = PDF.PDFParser(image_fd)
        while p.next_token() != "RESET_STATE" and p.error < 10:
            pass
        p.START_XREF(None, None)
        total_xrefs[xref] = p.pdf

    # Parse PDF 1.5+ compressed xref stream objects (/Type /XRef)
    # (skipped entirely under --no-xrefstream, for C1 ablation)
    for xh in ([] if options.no_xrefstream else hits_local.get('XREFSTREAM', [])):
        back = max(0, xh - 4096)
        image_fd.seek(back)
        window = image_fd.read(xh - back + 16)
        headers = list(re.finditer(rb'(\d+) (\d+) obj', window))
        if not headers:
            continue
        obj_off = back + headers[-1].start()
        if obj_off in total_xrefs:
            continue
        image_fd.seek(obj_off)
        p = PDF.PDFParser(image_fd)
        for _ in range(200000):
            tok = p.next_token()
            if tok and 'OBJECT_DECLERATION_END' in tok:
                break
            if (tok is None and p.empty()) or p.error > 50:
                break
        n_usable = sum(1 for e in p.pdf.xref if e[3] == 'n')
        if n_usable >= 4:
            print("[C1] xref stream object at %d (/XRef @ %d) -> %d entries, "
                  "%d usable" % (obj_off, xh, len(p.pdf.xref), n_usable))
            total_xrefs[obj_off] = p.pdf
        elif p.pdf.xref:
            print("[C1] xref stream at %d decoded %d entries but only %d "
                  "usable -- declined"
                  % (obj_off, len(p.pdf.xref), n_usable))

    # Fallback to boundary-based scan when no usable xref found
    usable = any(len(pdf.xref) > 0 for pdf in total_xrefs.values())
    if not usable:
        print("[E4] no usable xref table -- falling back to document scan")
        docs = segment_documents(hits_local)
        print("[E4] segmented %d document(s) from %%PDF-/%%%%EOF markers" % len(docs))
        base = os.path.basename(args[0])
        for doc_id, (doc_start, doc_end, n_obj) in enumerate(docs):
            out, size = carve_document_direct(image_fd, base, doc_id,
                                              doc_start, doc_end)
            result = verify.verify_pdf_file(out)
            if result['ok']:
                print("[E4]   doc%d  [%d,%d]  %d bytes  %d objects -> %s  "
                      "[E5 valid]"
                      % (doc_id, doc_start, doc_end, size, n_obj, out))
            else:
                rejected = out[:-len('.pdf')] + '.rejected'
                os.replace(out, rejected)
                print("[E4]   doc%d  [%d,%d]  %d bytes  %d objects -> %s  "
                      "[E5 SUSPECT: %s]"
                      % (doc_id, doc_start, doc_end, size, n_obj, rejected,
                         result['reason']))

    # V3 Improvement 2: synthetic xref for PDF boundaries with no xref coverage.
    # Happens when a file was deleted and its xref table was overwritten, but the
    # object bodies (N M obj ... endobj) are still intact on disk.
    _pdf_hdrs = sorted(hits_local.get('PDFHEADER', []))
    _pdf_eofs = sorted(hits_local.get('EOF', []))
    _obj_hits = hits_local.get('OBJECTS', [])

    # Which %PDF- starts already have a xref mapped to them?
    _covered = set()
    for _xp in total_xrefs:
        _pre = [h for h in _pdf_hdrs if h <= _xp]
        if _pre:
            _covered.add(max(_pre))

    for _i, _h in enumerate(_pdf_hdrs):
        if _h in _covered:
            continue
        _h_next = _pdf_hdrs[_i + 1] if _i + 1 < len(_pdf_hdrs) else None
        _lo = bisect.bisect_right(_pdf_eofs, _h)
        _hi = (bisect.bisect_left(_pdf_eofs, _h_next)
               if _h_next is not None else len(_pdf_eofs))
        if _lo >= _hi:
            continue
        _pdf_end = _pdf_eofs[_lo] + len(b'%%EOF')
        _synth = _build_synthetic_xref(image_fd, _h, _pdf_end, _obj_hits)
        if _synth is None:
            continue
        if _h not in total_xrefs:
            print("[V3] synthetic xref for PDF at %d: %d objects recovered"
                  % (_h, len(_synth.xref)))
            total_xrefs[_h] = _synth

    # Merge incremental-update xref nodes and emit only non-superseded maps
    superseded = set()
    for x in list(total_xrefs.keys()):
        for xref_offset in total_xrefs[x].xref_offsets:
            for y in list(total_xrefs.keys()):
                if y > x:
                    continue
                if xref_offset % SECTOR_SIZE == y % SECTOR_SIZE + 1:
                    if (total_xrefs[y].xref_range[0] == total_xrefs[x].xref_range[1]
                            or total_xrefs[y].xref_range[1] == total_xrefs[x].xref_range[0]):
                        new_pdf = PDF.PDFFile()
                        new_pdf.xref = total_xrefs[x].xref + total_xrefs[y].xref
                        new_pdf.xref_range = [
                            min(total_xrefs[y].xref_range[0], total_xrefs[x].xref_range[0]),
                            max(total_xrefs[y].xref_range[1], total_xrefs[x].xref_range[1]),
                        ]
                        save_map("%s-%s" % (x, y), new_pdf, image_fd, hits_local)
                        superseded.add(x)
                        superseded.add(y)

    for x, pdf in total_xrefs.items():
        if x in superseded:
            print("[E6] node %s superseded by a merged map -- skipped" % x)
            continue
        save_map(x, pdf, image_fd, hits_local)

    image_fd.close()


if options.create:
    build_index()
elif options.maps:
    print_xrefs()
elif getattr(options, 'print'):
    with open(options.index, 'rb') as fd:
        hits = pickle.load(fd)
    print(hits)
else:
    print("Nothing to do... try -h")
