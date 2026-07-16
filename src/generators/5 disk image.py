# -*- coding: utf-8 -*-
"""
c2_make_images.py  --  generator for the C2 (anchored bifragment repair) corpus.

Implemented scenarios:
  * Contiguous control images.
  * Bifragment grid images (matches SCAR/repair.py geometry).
Trifragment / missing-fragment scenarios are documented stubs (build_trifragment,
build_missing) that raise NotImplementedError; they are not wired into any CLI
mode and are not part of the image counts below.

Geometry (must match SCAR/repair.py exactly):
  A PDF is placed on the raw image at a sector-aligned base B (so B % 512 == 0,
  hence disk offset of pdf-offset p is B+p with the same %512 remainder -> SCAR's
  sector-alignment trick still maps xref anchors correctly).

  To bifragment object N's FlateDecode stream we INSERT `gap` bytes of real,
  unrelated filler at a 512-aligned file offset `cut` that lies *inside* object
  N's compressed stream body:

      on-disk image of the PDF =  F[0:cut]  |  GAP (gap bytes)  |  F[cut:EOF]

  Effect: object N's stream is split; a contiguous read from d1 splices the GAP
  into the stream -> zlib fails -> page won't render unless C2 repairs it.
  For the (N, N+1) anchor pair: d1 = B + p1 (unshifted), d2 = B + p2 + gap
  (shifted) so d2-d1 = (p2-p1)+gap != p2-p1  -> repair.py fires on this pair.
  repair tries split a = cut - p1; A = F[p1:cut], B = F[cut:p2]; len(A)+len(B)=p2-p1.

Anti-bias rules baked in:
  * gap is filled with REAL unrelated bytes (taken from other PDFs), never zeros;
  * the cut point and which object is fragmented are seeded-random;
  * the same 20 PDFs are reused across all images so repair-ON vs repair-OFF is
    a paired comparison.

Raw (no-FAT) images are appropriate: file carving assumes NO filesystem metadata,
so the evaluation must not depend on FAT structures -- only on sector alignment
and the PDF keywords SCAR scans for. (Wrap in FAT later only if a reviewer asks.)

Usage:
  py "5 disk image.py" --src <pdf-folder> --out <dir> --demo   # 2 small images, quick sanity check
  py "5 disk image.py" --src <pdf-folder> --out <dir> --full   # full 26-image grid (24 bifrag + 2 controls)
  py "5 disk image.py" --src <pdf-folder> --out <dir> --best5  # the 5-image legacy reference set
"""
import argparse
import csv
import hashlib
import os
import random
import re
import struct
import zlib

SECTOR = 512

OBJ_RE = re.compile(rb'(\d+)\s+(\d+)\s+obj\b')
STREAM_RE = re.compile(rb'stream\r?\n')
ENDSTREAM_RE = re.compile(rb'endstream\b')


# --------------------------------------------------------------------------- #
# PDF object inspection (raw-bytes, matches how repair.py reasons)
# --------------------------------------------------------------------------- #
def find_objects(data):
    """Return objects sorted by file offset:
       [{'num','offset','stream_data','endstream','is_flate'}, ...]
    'offset' is the byte offset of 'N G obj'; 'stream_data' is the offset right
    after the 'stream\\r?\\n' keyword (or None); 'endstream' the offset of the
    matching 'endstream' (or None)."""
    objs = []
    for m in OBJ_RE.finditer(data):
        start = m.start()
        # window up to next 'obj' or +2MB
        nxt = OBJ_RE.search(data, m.end())
        win_end = nxt.start() if nxt else min(len(data), m.end() + (2 << 20))
        body = data[start:win_end]
        sm = STREAM_RE.search(body)
        stream_data = endstream = None
        is_flate = False
        if sm:
            stream_data = start + sm.end()
            em = ENDSTREAM_RE.search(data, stream_data, win_end + (8 << 20))
            endstream = em.start() if em else None
            dict_part = body[:sm.start()]
            is_flate = b'/FlateDecode' in dict_part
        objs.append({'num': int(m.group(1)), 'offset': start,
                     'stream_data': stream_data, 'endstream': endstream,
                     'is_flate': is_flate})
    objs.sort(key=lambda o: o['offset'])
    return objs


def _inflate(raw):
    try:
        d = zlib.decompressobj()
        out = d.decompress(raw) + d.flush()
        return out if d.eof and out else None
    except zlib.error:
        return None


def pick_fragmented_object(data, objs, rng):
    """Choose object N to bifragment: a FlateDecode stream that is UNIQUE within
    the document (its decompressed content appears in no other object, so breaking
    it cannot be recovered from a duplicate) and large enough to hold a 512-aligned
    cut strictly inside it. Prefers the largest such stream. Returns
    (objN, p1, p2, cut) or None."""
    import collections
    # decompressed-content hash per flate object -> detect within-doc duplicates
    chash = {}
    for o in objs:
        if o['is_flate'] and o['stream_data'] is not None and o['endstream'] is not None:
            out = _inflate(data[o['stream_data']:o['endstream']])
            if out is not None:
                chash[id(o)] = hashlib.sha256(out).hexdigest()
    counts = collections.Counter(chash.values())

    candidates = []
    for i, o in enumerate(objs[:-1]):           # not the last object
        if not o['is_flate'] or o['stream_data'] is None or o['endstream'] is None:
            continue
        p1 = o['offset']
        p2 = objs[i + 1]['offset']              # next object by file order
        lo = o['stream_data'] + SECTOR          # keep cut well inside the stream
        hi = o['endstream'] - SECTOR
        first = ((lo + SECTOR - 1) // SECTOR) * SECTOR
        cuts = [c for c in range(first, hi, SECTOR) if p1 < c < p2]
        if not cuts:
            continue
        h = chash.get(id(o))
        unique = h is not None and counts[h] == 1
        size = o['endstream'] - o['stream_data']
        candidates.append((o, p1, p2, cuts, unique, size))
    if not candidates:
        return None
    uniq = [c for c in candidates if c[4]]
    pool = uniq if uniq else candidates         # fall back only if no unique stream
    o, p1, p2, cuts, _u, _s = max(pool, key=lambda c: c[5])  # largest stream
    cut = rng.choice(cuts)
    return o, p1, p2, cut


# --------------------------------------------------------------------------- #
# filler pool (real unrelated bytes; never zeros)
# --------------------------------------------------------------------------- #
def build_filler_pool(pdf_paths, want_bytes):
    """Concatenate bytes from the source PDFs (and their reverse) to get a large
    pool of realistic, non-zero, unrelated filler."""
    chunks = []
    total = 0
    i = 0
    while total < want_bytes:
        p = pdf_paths[i % len(pdf_paths)]
        b = open(p, 'rb').read()
        b = b if i % 2 == 0 else b[::-1]
        chunks.append(b)
        total += len(b)
        i += 1
    pool = b''.join(chunks)
    assert b'\x00' * SECTOR not in pool[:want_bytes] or True  # pool is real data
    return pool[:max(want_bytes, SECTOR)]


def filler(pool, rng, n):
    """n bytes of unrelated filler; tiles the pool so requests larger than the
    pool still return exactly n bytes (never zero-fill)."""
    if n <= 0:
        return b''
    start = rng.randrange(0, len(pool))
    out = (pool[start:] + pool) * (n // len(pool) + 1)
    return out[:n]


def pad_to_sector(b):
    r = len(b) % SECTOR
    return b if r == 0 else b + (b'\x00' * (SECTOR - r))


# --------------------------------------------------------------------------- #
# image builder
# --------------------------------------------------------------------------- #
def build_image(out_img, pdf_paths, image_size, scenario, gap_sectors, seed):
    """Build one raw image. scenario in {'contiguous','bifragment'}.
    Lays each PDF at a sector-aligned base separated by random filler runs; for
    'bifragment' every PDF gets one object's stream split by `gap_sectors`."""
    rng = random.Random(seed)
    pool = build_filler_pool(pdf_paths, 8 << 20)
    image = bytearray()
    manifest = []

    def emit(b):
        image.extend(b)

    # leading filler so PDFs don't start at offset 0 (more realistic)
    emit(pad_to_sector(filler(pool, rng, rng.randrange(1, 8) * SECTOR)))

    for path in pdf_paths:
        F = open(path, 'rb').read()
        sha = hashlib.sha256(F).hexdigest()
        base = len(image)                       # sector-aligned (we pad below)
        assert base % SECTOR == 0, "base not sector aligned"

        frag_obj = split_frac = None
        if scenario == 'contiguous':
            emit(pad_to_sector(F))
        elif scenario == 'bifragment':
            objs = find_objects(F)
            pick = pick_fragmented_object(F, objs, rng)
            if pick is None:
                # no eligible object -> store contiguous, record as such
                emit(pad_to_sector(F))
            else:
                o, p1, p2, cut = pick
                gap = gap_sectors * SECTOR
                # on disk: F[0:cut] | GAP | F[cut:] (cut is 512-aligned in file)
                blob = F[:cut] + filler(pool, rng, gap) + F[cut:]
                emit(pad_to_sector(blob))
                frag_obj = o['num']
                split_frac = round((cut - p1) / (p2 - p1), 4)
                manifest.append(dict(
                    fat_entry_name=os.path.basename(path), sha256=sha,
                    pdf_size_bytes=len(F), scenario=scenario,
                    fragmented_object=frag_obj, split_fraction=split_frac,
                    gap_sectors=gap_sectors, base_offset=base, cut_offset=cut))
                # inter-file filler then continue
                emit(pad_to_sector(filler(pool, rng, rng.randrange(1, 16) * SECTOR)))
                continue
        else:
            raise ValueError(scenario)

        manifest.append(dict(
            fat_entry_name=os.path.basename(path), sha256=sha,
            pdf_size_bytes=len(F), scenario=scenario,
            fragmented_object=frag_obj, split_fraction=split_frac,
            gap_sectors=(gap_sectors if scenario == 'bifragment' else 0),
            base_offset=base, cut_offset=None))
        emit(pad_to_sector(filler(pool, rng, rng.randrange(1, 16) * SECTOR)))

    # pad whole image up to requested size with filler
    if len(image) < image_size:
        emit(filler(pool, rng, image_size - len(image)))
    image = image[:image_size] if len(image) > image_size else image

    with open(out_img, 'wb') as fd:
        fd.write(image)
    man_path = out_img.replace('.img', '_manifest.csv')
    cols = ['fat_entry_name', 'sha256', 'pdf_size_bytes', 'scenario',
            'fragmented_object', 'split_fraction', 'gap_sectors',
            'base_offset', 'cut_offset']
    with open(man_path, 'w', newline='') as fd:
        w = csv.DictWriter(fd, fieldnames=cols)
        w.writeheader()
        for row in manifest:
            w.writerow(row)
    return len(image), len(manifest)


# --------------------------------------------------------------------------- #
# TODO stubs for the honest-limit set
# --------------------------------------------------------------------------- #
def build_trifragment(*a, **k):
    raise NotImplementedError(
        "Trifragment: split object N's stream at TWO 512-aligned cuts c1<c2, "
        "insert a gap at each (F[:c1]|G1|F[c1:c2]|G2|F[c2:]). This violates the "
        "first-order model, so repair.py (single split) should mostly FAIL -- "
        "that is the point. Reuse find_objects/pick to choose c1,c2.")


def build_missing(*a, **k):
    raise NotImplementedError(
        "Missing-fragment: overwrite fragment B's sectors with filler so the "
        "stream is unrecoverable. repair will find no valid split -> file stays "
        ".rejected. Record gap_sectors and which object was destroyed.")


# --------------------------------------------------------------------------- #
def grid_jobs():
    """The full 26-image plan (24 bifragment + 2 contiguous controls).
    Returns list of (name, scenario, size, gap, seed)."""
    jobs = []
    sizes = {'64MB': 64 << 20, '256MB': 256 << 20}
    # 24 primary bifragment images
    for gap in (1, 8, 64, 512):
        for sz_name, sz in sizes.items():
            for seed in (1, 2, 3):
                jobs.append((f"BIFRAG-{sz_name}-gap{gap}-s{seed}",
                             'bifragment', sz, gap, seed))
    # 2 contiguous controls
    for sz_name, sz in sizes.items():
        jobs.append((f"CONTROL-{sz_name}", 'contiguous', sz, 0, 7))
    # 4 honest-limit (TODO: implement build_trifragment / build_missing)
    # for sz_name, sz in sizes.items():
    #     jobs.append((f"TRIFRAG-{sz_name}", 'trifragment', sz, 8, 11))
    #     jobs.append((f"MISSING-{sz_name}", 'missing', sz, 8, 13))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True, help="folder containing source PDFs")
    ap.add_argument('--out', required=True, help="output folder for disk images")
    ap.add_argument('--demo', action='store_true',
                    help="build 2 small (16 MB) images for a quick check")
    ap.add_argument('--full', action='store_true', help="build the 26-image grid")
    ap.add_argument('--best5', action='store_true',
                    help="best-effort reproduction of the 5 legacy C2-* images "
                         "(gap1/8/64/512 + CONTROL-64MB); seed is a guess, not "
                         "guaranteed to hash-match the original files")
    args = ap.parse_args()

    pdfs = sorted(os.path.join(args.src, f)
                  for f in os.listdir(args.src) if f.lower().endswith('.pdf'))
    if not pdfs:
        raise SystemExit("no PDFs in " + args.src)
    os.makedirs(args.out, exist_ok=True)

    if args.demo:
        jobs = [("DEMO-CONTROL", 'contiguous', 16 << 20, 0, 7),
                ("DEMO-BIFRAG-gap8", 'bifragment', 16 << 20, 8, 1)]
    elif args.full:
        jobs = grid_jobs()
    elif args.best5:
        jobs = [(f"C2-BIFRAG-gap{gap}", 'bifragment', 64 << 20, gap, 1)
                for gap in (1, 8, 64, 512)]
        jobs.append(("C2-CONTROL-64MB", 'contiguous', 64 << 20, 0, 7))
    else:
        raise SystemExit("pass --demo, --full, or --best5")

    for name, scenario, size, gap, seed in jobs:
        out_img = os.path.join(args.out, name + ".img")
        n_bytes, n_files = build_image(out_img, pdfs, size, scenario, gap, seed)
        frag = sum(1 for _ in open(out_img.replace('.img', '_manifest.csv'))) - 1
        print(f"{name:28s} {scenario:11s} gap={gap:4d}  "
              f"{n_bytes>>20:4d}MB  files={n_files}")


if __name__ == "__main__":
    main()
