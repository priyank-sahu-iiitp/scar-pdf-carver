#!/usr/bin/env python3
"""
SCAR — semantic PDF fragment carver (CLI, scar engine only).

Recovers PDFs from a raw disk image using the cross-reference-driven
semantic pipeline: structural index -> anchor derivation (classic xref,
xref-stream, synthetic xref, header->EOF fallback) -> reconstruction ->
bi-fragment repair -> graded validation -> content dedup.

This is the strict SCAR build: the signature carver and the GUI are not
included. Every recovery is attributable to the semantic engine.
"""
import argparse
import collections
import glob
import hashlib
import os
import pickle
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_PY = sys.executable

# Stage progress weights
_W_INDEX = 0.35
_W_MAPS = 0.05
_W_FORCE = 0.60


class Progress:
    # Animated one-line progress bar with rate and ETA
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.pct = 0.0
        self.stage = ""
        self.detail = ""
        self._hist = collections.deque()
        self._t0 = time.perf_counter()
        self._last_draw = 0.0
        self._is_tty = sys.stdout.isatty()
        self._last_pct_i = -1

    def update(self, pct, stage=None, detail=None, force=False):
        self.pct = max(self.pct, min(pct, 100.0))
        if stage is not None:
            self.stage = stage
        if detail is not None:
            self.detail = detail
        now = time.perf_counter()
        self._hist.append((now, self.pct))
        while self._hist and now - self._hist[0][0] > 3.0:
            self._hist.popleft()
        if not force and now - self._last_draw < 0.25:
            return
        self._last_draw = now
        self._draw(now)

    def _rate(self, now):
        if len(self._hist) < 2:
            return 0.0
        t0, p0 = self._hist[0]
        dt = now - t0
        return (self.pct - p0) / dt if dt > 0 else 0.0

    def _draw(self, now):
        if self.quiet:
            return
        if self._is_tty:
            rate = self._rate(now)
            eta = (100.0 - self.pct) / rate if rate > 0.01 else None
            filled = int(self.pct / 100 * 28)
            bar = "#" * filled + "-" * (28 - filled)
            eta_s = ("ETA %ds" % int(eta)) if eta is not None and eta < 1e5 else "ETA --"
            line = ("\r[%s] %5.1f%%  %+5.1f%%/s  %-22s %s   "
                    % (bar, self.pct, rate, (self.stage + " " + self.detail).strip(),
                       eta_s))
            sys.stdout.write(line)
            sys.stdout.flush()
        else:
            i = int(self.pct)
            if i != self._last_pct_i:
                self._last_pct_i = i
                sys.stdout.write("PCT:%d\n" % i)
                sys.stdout.flush()

    def done(self, msg=""):
        self.pct = 100.0
        self._draw(time.perf_counter())
        if not self.quiet:
            elapsed = time.perf_counter() - self._t0
            if self._is_tty:
                sys.stdout.write("\n%s(%.1fs total)\n"
                                 % (msg + "  " if msg else "", elapsed))
            else:
                sys.stdout.write("PCT:100\nDONE:%.1f\n" % elapsed)
            sys.stdout.flush()


# Stage 1: build keyword index from disk image
def stage_index(disk, idx, prog):
    cmd = [_PY, os.path.join(_HERE, "pdf_carver.py"),
           "-i", idx, "-c", "-P", disk]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            bufsize=1)
    for line in proc.stdout:
        if line.startswith("[PROGRESS]"):
            try:
                done, total = (int(x) for x in line.split()[1:3])
            except ValueError:
                continue
            frac = done / total if total else 0.0
            prog.update(frac * _W_INDEX * 100, stage="stage 1/3 index",
                        detail="%d/%d MB" % (done >> 20, total >> 20))
    proc.wait()
    if proc.returncode not in (0, None):
        sys.exit("\nindex build failed (exit %d)" % proc.returncode)
    prog.update(_W_INDEX * 100, stage="stage 1/3 index", force=True)


# Stage 2: parse xref regions and produce map files
def stage_maps(disk, idx, out, prog, no_xrefstream=False):
    prog.update(_W_INDEX * 100, stage="stage 2/3 maps",
                detail="parsing xref regions", force=True)
    cmd = [_PY, os.path.join(_HERE, "pdf_carver.py"), "-i", idx, "-m", disk]
    if no_xrefstream:
        cmd.append("-X")
    r = subprocess.run(cmd, cwd=out, capture_output=True, text=True)
    if r.returncode != 0:
        pass
    prog.update((_W_INDEX + _W_MAPS) * 100, stage="stage 2/3 maps",
                detail="done", force=True)


# Stage 2.5: direct-carve PDFs whose xref streams reference object streams (/ObjStm)
# These PDFs use PDF 1.5+ /ObjStm to compress objects; the standard map-based
# reconstruction skips type-2 xref entries, so this stage catches them by direct
# byte-range extraction using the keyword index for boundaries.
def stage_objstm(disk, idx, out, prog):
    if not os.path.exists(idx):
        return 0

    sys.path.insert(0, _HERE)
    import PDF as _PDF

    with open(idx, 'rb') as f:
        hits = pickle.load(f)

    xstream_hits = sorted(hits.get('XREFSTREAM', []))
    pdf_headers  = sorted(hits.get('PDFHEADER', []))
    eof_hits     = sorted(hits.get('EOF', []))

    if not xstream_hits:
        return 0

    _EOF_LEN = len(b'%%EOF')
    carved    = 0
    seen_ranges = set()

    prog.update((_W_INDEX + _W_MAPS) * 100, stage="stage 2.5 objstm",
                detail="scanning %d xref streams" % len(xstream_hits), force=True)

    with open(disk, 'rb') as image_fd:
        for xh in xstream_hits:
            # Scan back to find the enclosing "N M obj" header for this /XRef keyword
            back = max(0, xh - 4096)
            image_fd.seek(back)
            window = image_fd.read(xh - back + 16)
            obj_matches = list(re.finditer(rb'(\d+)\s+(\d+)\s+obj', window))
            if not obj_matches:
                continue
            obj_off = back + obj_matches[-1].start()

            # Parse the xref stream object to count type-2 (object stream) entries
            image_fd.seek(obj_off)
            p = _PDF.PDFParser(image_fd)
            for _ in range(200000):
                tok = p.next_token()
                if tok and 'OBJECT_DECLERATION_END' in tok:
                    break
                if (tok is None and p.empty()) or p.error > 50:
                    break

            n_type2 = sum(1 for e in p.pdf.xref if e[3] == 's')
            if n_type2 == 0:
                continue  # no object streams — standard map path handles this PDF

            # Find nearest %PDF- header before this xref stream object
            pre_headers = [h for h in pdf_headers if h < obj_off]
            if not pre_headers:
                continue
            pdf_start = max(pre_headers)

            # Find nearest %%EOF after this xref stream object
            post_eofs = [e for e in eof_hits if e > obj_off]
            if not post_eofs:
                continue
            pdf_end = min(post_eofs) + _EOF_LEN + 2  # +2 for trailing \r\n

            size = pdf_end - pdf_start
            if size <= 0 or size > 300 * 1024 * 1024:
                continue

            key = (pdf_start, pdf_end)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)

            image_fd.seek(pdf_start)
            data = image_fd.read(size)
            if not data:
                continue

            # Write as .objscan.pdf so stage_reconstruct picks it up
            out_path = os.path.join(out, 'objstm_%d.objscan.pdf' % pdf_start)
            with open(out_path, 'wb') as f:
                f.write(data)
            carved += 1

    prog.update((_W_INDEX + _W_MAPS) * 100, stage="stage 2.5 objstm",
                detail="%d direct-carved" % carved, force=True)
    return carved


# Stage 3: reconstruct, repair, verify, and deduplicate PDFs
def stage_reconstruct(disk, out, prog, repair_on=True):
    sys.path.insert(0, _HERE)
    import verify
    import repair

    maps = sorted(glob.glob(os.path.join(out, "*.map")))
    objscan = sorted(glob.glob(os.path.join(out, "*.objscan.pdf")))
    base = (_W_INDEX + _W_MAPS) * 100
    span = _W_FORCE * 100
    n = len(maps) + len(objscan)
    if n == 0:
        prog.update(100, stage="stage 3/3", detail="no recoverable PDFs")
        return 0, 0, 0, 0

    seen = {}
    ok = dup = suspect = failed = 0
    step = 0

    def advance(detail):
        prog.update(base + span * step / n, stage="stage 3/3 reconstruct",
                    detail=detail)

    for m in maps:
        step += 1
        pdf = m[:-4] + ".pdf"
        r = subprocess.run([_PY, os.path.join(_HERE, "PDFForcer.py"),
                            "-m", os.path.basename(m),
                            "-o", os.path.basename(pdf), disk],
                           cwd=out, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(pdf):
            failed += 1
            advance("%d/%d  (skip)" % (step, n))
            continue
        # Repair bifragmented streams before verifying (C2). Skippable for ablation.
        if repair_on:
            try:
                nfix = repair.repair_pdf(disk, m, pdf)
                if nfix:
                    with open(os.path.join(out, "_c9_repairs.txt"), "a") as lg:
                        lg.write("%s\t%d\n" % (os.path.basename(pdf), nfix))
            except Exception:
                pass
        res = verify.verify_pdf_file(pdf)
        if not res['ok']:
            _replace_with_retry(pdf, pdf[:-4] + ".rejected")
            suspect += 1
            advance("%d/%d  (suspect)" % (step, n))
            continue
        h = hashlib.sha256(open(pdf, 'rb').read()).hexdigest()
        if h in seen:
            _replace_with_retry(pdf, pdf[:-4] + ".duplicate")
            dup += 1
            advance("%d/%d  (duplicate)" % (step, n))
        else:
            frag_dir = os.path.join(out, 'fragmentcarve')
            os.makedirs(frag_dir, exist_ok=True)
            final = os.path.join(frag_dir, os.path.basename(pdf))
            _replace_with_retry(pdf, final)
            seen[h] = final
            ok += 1
            advance("%d/%d  (%d ok)" % (step, n, ok))

    # Deduplicate objscan results
    for p in objscan:
        step += 1
        h = hashlib.sha256(open(p, 'rb').read()).hexdigest()
        if h in seen:
            _replace_with_retry(p, p[:-4] + ".duplicate")
            dup += 1
        else:
            frag_dir = os.path.join(out, 'fragmentcarve')
            os.makedirs(frag_dir, exist_ok=True)
            final = os.path.join(frag_dir, os.path.basename(p))
            _replace_with_retry(p, final)
            seen[h] = final
            ok += 1
        advance("%d/%d  (%d ok)" % (step, n, ok))

    return ok, dup, suspect, failed


# Move intermediate files to _work/ so output folder shows only carved PDFs
def stage_cleanup(out, prog):
    work = os.path.join(out, '_work')
    os.makedirs(work, exist_ok=True)
    patterns = ('*.map', '*.idx', '*.rejected', '*.duplicate', '_c9_repairs.txt')
    moved = 0
    for pat in patterns:
        for src in glob.glob(os.path.join(out, pat)):
            dst = os.path.join(work, os.path.basename(src))
            _replace_with_retry(src, dst)
            moved += 1
    prog.update(100, stage="cleanup", detail="%d files -> _work/" % moved, force=True)


# Stage 4: strict P1-P6 verify on all carved PDFs
def stage_verify(out, prog):
    sys.path.insert(0, _HERE)
    import verifier

    targets = sorted(glob.glob(os.path.join(out, 'fragmentcarve', '*.pdf')))
    if not targets:
        return []

    results = []
    n = len(targets)
    for i, path in enumerate(targets, 1):
        prog.update(100, stage="stage 4/5 verify",
                    detail="%d/%d  %s" % (i, n, os.path.basename(path)),
                    force=True)
        r = verifier.verify(path)
        results.append(r)

    prog.update(100, stage="stage 4/5 verify",
                detail="%d files done" % n, force=True)
    return results


# Stage 5: content-aware dedup across all verified outputs; writes dedup_results.csv
def stage_dedup(verify_results, out, prog):
    sys.path.insert(0, _HERE)
    import dedup as dedup_mod

    if not verify_results:
        return [], {}

    prog.update(100, stage="stage 5/5 dedup",
                detail="running", force=True)
    labeled = dedup_mod.dedup(verify_results, quiet=True)
    counts  = dedup_mod.summarise(labeled, quiet=True)

    # Write per-file CSV
    csv_path = os.path.join(out, "dedup_results.csv")
    try:
        with open(csv_path, 'w') as fd:
            fd.write("file,source,verdict,dedup_label,dedup_reason,"
                     "pages,failed_refs,failed_streams,render_pages\n")
            for r in labeled:
                fname = os.path.basename(r.get('path', ''))
                src = _carver_source(fname)
                fd.write("%s,%s,%s,%s,%s,%d,%d,%d,%d\n" % (
                    fname,
                    src,
                    r.get('verdict', ''),
                    r.get('dedup_label', ''),
                    r.get('dedup_reason', '').replace(',', ';'),
                    r.get('pages', 0),
                    r.get('failed_refs', 0),
                    r.get('failed_streams', 0),
                    r.get('render_pages', 0),
                ))
    except Exception as e:
        print("WARNING: could not write CSV: %s" % e)

    prog.update(100, stage="stage 5/5 dedup",
                detail="done", force=True)
    return labeled, counts


def _carver_source(fname):
    """Return carver tag from filename: OBJSTM | OBJSCAN | V1_XREF."""
    if fname.startswith('objstm_'):
        return 'OBJSTM'
    if fname.endswith('.objscan.pdf'):
        return 'OBJSCAN'
    return 'V1_XREF'


def _divider():
    print("-" * 56)


# Evidence integrity: hash the source disk image before and after analysis so
# a run leaves an auditable record that the tool did not alter the evidence.
# This does not itself constitute chain of custody (that covers acquisition,
# storage, and handling before the image reaches this tool) -- it only
# supports it, by proving this tool's own read-only access claim in practice.
def _replace_with_retry(src, dst, attempts=6, delay=0.3):
    """os.replace() wrapper that retries past transient Windows file locks
    (PermissionError / WinError 32 -- typically Defender or the Search
    Indexer briefly holding a handle open right after a write). Observed
    under concurrent multi-image batch runs (~2% of runs in a 10-way
    concurrent batch); a single carve.py run finishes fast enough that a
    short backoff is enough to ride out the lock."""
    last_err = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay * (i + 1))
    raise last_err


def _sha256_file(path, chunk_size=1 << 24):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            h.update(chunk)
    return h.hexdigest()


def log_evidence_hash(disk, out, label):
    work = os.path.join(out, '_work')
    os.makedirs(work, exist_ok=True)
    log_path = os.path.join(work, 'evidence_hash.log')
    print("[%s] hashing evidence image..." % label)
    t0 = time.perf_counter()
    digest = _sha256_file(disk)
    elapsed = time.perf_counter() - t0
    ts = datetime.now(timezone.utc).isoformat()
    with open(log_path, 'a') as f:
        f.write("%s  %-14s  sha256=%s  path=%s  elapsed=%.1fs\n"
                % (ts, label, digest, disk, elapsed))
    print("[%s] sha256=%s  (%.1fs)" % (label, digest, elapsed))
    return digest


def main():
    ap = argparse.ArgumentParser(
        description="SCAR - recover PDFs from a raw disk image "
                    "(semantic fragment engine).")
    ap.add_argument("disk", help="disk image (.dd / .img / raw)")
    ap.add_argument("--out", default="carved_pdfs", help="output directory")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the progress bar")
    ap.add_argument("--no-repair", action="store_true",
                    help="disable C2 anchored bi-fragment repair (ablation)")
    ap.add_argument("--no-xrefstream", action="store_true",
                    help="disable C1 compressed xref-stream/ObjStm decoding "
                         "(ablation: reproduces pre-C1 classic-xref-only capability)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="disable content-aware dedup, keep all raw reconstructions (ablation)")
    args = ap.parse_args()

    disk = os.path.abspath(args.disk)
    out  = os.path.abspath(args.out)
    if not os.path.exists(disk):
        sys.exit("ERROR: not found: %s" % disk)
    os.makedirs(out, exist_ok=True)
    idx = os.path.join(out, "index.idx")

    # ── Evidence integrity: hash before any analysis touches the image ─
    pre_hash = log_evidence_hash(disk, out, "PRE-ANALYSIS")

    prog = Progress(quiet=args.quiet)

    # ── Stage 1: build index ──────────────────────────────────────────
    stage_index(disk, idx, prog)

    # ── Stages 2-3: xref maps + reconstruct ───────────────────────────
    stage_maps(disk, idx, out, prog, no_xrefstream=args.no_xrefstream)
    objstm_n = 0 if args.no_xrefstream else stage_objstm(disk, idx, out, prog)
    ok, dup, suspect, failed = stage_reconstruct(disk, out, prog,
                                                 repair_on=not args.no_repair)

    # ── Move intermediates out of the way ─────────────────────────────
    stage_cleanup(out, prog)

    # ── Post-processing: strict verify + content-aware dedup ──────────
    verify_results = stage_verify(out, prog)
    if args.no_dedup:
        labeled, dedup_counts = [], {}
    else:
        labeled, dedup_counts = stage_dedup(verify_results, out, prog)

    prog.done()

    # ── Evidence integrity: hash again, confirm nothing changed ───────
    post_hash = log_evidence_hash(disk, out, "POST-ANALYSIS")
    if pre_hash != post_hash:
        print("WARNING: evidence image hash changed during analysis! "
              "pre=%s post=%s" % (pre_hash, post_hash), file=sys.stderr)
    else:
        print("Evidence integrity verified: image hash unchanged (%s)" % pre_hash)

    # ── Structured summary ────────────────────────────────────────────
    _divider()
    print("  Carve")
    print("    V1 xref-driven : %d valid  %d dup  %d suspect  %d skipped"
          % (ok, dup, suspect, failed))
    print("    ObjStm carver  : %d direct-carved  (type-2 /ObjStm PDFs)" % objstm_n)

    if verify_results:
        sys.path.insert(0, _HERE)
        from verifier import CONFIRMED, PARTIAL, REJECT
        n_c = sum(1 for r in verify_results if r['verdict'] == CONFIRMED)
        n_p = sum(1 for r in verify_results if r['verdict'] == PARTIAL)
        n_r = sum(1 for r in verify_results if r['verdict'] == REJECT)
        _divider()
        print("  Strict verify  (P1-P6)")
        print("    CONFIRMED : %d" % n_c)
        print("    PARTIAL   : %d" % n_p)
        print("    REJECT    : %d" % n_r)

    if dedup_counts:
        from dedup import UNIQUE, EXACT_DUP, LOGICAL_DUP, CONTAINMENT, CORRUPT
        _divider()
        print("  Content-aware dedup")
        print("    UNIQUE      : %d" % dedup_counts.get(UNIQUE, 0))
        print("    EXACT_DUP   : %d" % dedup_counts.get(EXACT_DUP, 0))
        print("    LOGICAL_DUP : %d" % dedup_counts.get(LOGICAL_DUP, 0))
        print("    CONTAINMENT : %d" % dedup_counts.get(CONTAINMENT, 0))
        print("    CORRUPT     : %d" % dedup_counts.get(CORRUPT, 0))

        # Per-source breakdown: total carved vs unique kept
        src_groups = {}
        for r in labeled:
            src = _carver_source(os.path.basename(r.get('path', '')))
            g = src_groups.setdefault(src, {'total': 0, 'CONFIRMED': 0, 'PARTIAL': 0, 'deduped': 0})
            g['total'] += 1
            if r.get('dedup_label') == UNIQUE:
                g[r.get('verdict', 'PARTIAL')] = g.get(r.get('verdict', 'PARTIAL'), 0) + 1
            else:
                g['deduped'] += 1
        if src_groups:
            _divider()
            print("  Recoveries by carver  (unique | deduped/removed)")
            for src in ('V1_XREF', 'OBJSTM', 'OBJSCAN'):
                if src not in src_groups:
                    continue
                g = src_groups[src]
                unique_n = g['CONFIRMED'] + g['PARTIAL']
                print("    %-10s : %d carved  ->  %d unique  (%d CONFIRMED  %d PARTIAL  %d removed)" % (
                    src, g['total'], unique_n,
                    g['CONFIRMED'], g['PARTIAL'], g['deduped']))

    _divider()
    unique_n = dedup_counts.get('UNIQUE', ok) if dedup_counts else ok
    print("  Final unique recoveries : %d" % unique_n)
    print("  Fragment carved PDFs    : %s/fragmentcarve/" % out)
    print("  Intermediates           : %s/_work/" % out)
    if dedup_counts:
        print("  Results CSV             : %s/dedup_results.csv" % out)
    _divider()


if __name__ == "__main__":
    main()
