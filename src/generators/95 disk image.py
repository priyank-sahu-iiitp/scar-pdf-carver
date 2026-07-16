# -*- coding: utf-8 -*-
"""
make_frag_corpus.py -- systematic fragment-count corpus for PDF carver evaluation.

Produces RAW disk images (NO filesystem, 512-byte sectors) that vary ONE
controlled fragmented target across 5 classic carving scenarios and fragment
counts N = 2..20.  Everything else is held fixed, so (scenario, N) are the only
variables -- a clean controlled sweep for "does the tool reassemble N fragments".

5 scenarios (reusing the project's existing codes):
  SEQF  Sequential fragmentation     target -> N fragments, on disk IN pdf order,
                                      separated by gaps of real unrelated data.
  NSQF  Non-sequential fragmentation target -> N fragments, on disk in a
                                      NON-identity permutation (carver must reorder).
  MISS  Missing fragment(s)          target -> N fragments in order, but exactly ONE
                                      middle fragment is overwritten by unrelated
                                      data (gone) -> target is only partially present.
  NEST  Nested files                 target -> N fragments in order; the SECONDARY
                                      pdf sits ENTIRELY inside one gap (complete,
                                      contiguous). Both files are fully present.
  BRAID Braided files                target AND secondary each -> N fragments,
                                      interleaved F1 G1 F2 G2 ... Fn Gn. Both fully
                                      present, must be de-interleaved.

Design (matches c2_make_images.py conventions):
  * SECTOR = 512; every fragment / file starts at a sector-aligned disk offset.
  * Internal split points are multiples of SECTOR, so concatenating a target's
    fragments in pdf order reproduces the original file byte-for-byte (verified).
  * Gaps are filled with REAL unrelated bytes (other PDFs, fwd+reversed), never
    zeros -> no anti-pattern a carver could exploit.
  * One fixed target (Linearized.pdf) and one fixed partner (1.0.pdf), both with
    unique byte-sizes so exact-byte-size ground truth never collides on them.
  * All 40 source PDFs appear once per image; non-target/partner are contiguous
    distractors (realistic load + false-positive opportunities).

Outputs per image  <SCEN>_fNN  :
  <name>.img             raw image (default 64 MiB)
  <name>_manifest.csv    one row per placed PDF; has pdf_size_bytes so it plugs
                         straight into eval/groundtruth_c9.py
  <name>_fragments.csv   per-fragment geometry (pdf byte-range -> disk offset)
plus a corpus-wide INDEX.csv.

Usage:
  py make_frag_corpus.py --demo            # SEQF/BRAID at N=2,3 into <out>\\_demo, then verify
  py make_frag_corpus.py --full            # all 5 scenarios x N=2..20  (95 images)
  py make_frag_corpus.py --verify [--full|--demo]   # reconstruct & sha256-check
"""
import argparse
import csv
import glob
import hashlib
import os
import random

SECTOR = 512

TARGET_NAME = "Linearized.pdf"     # primary fragmented file (unique size, 27 pp)
SECONDARY_NAME = "1.0.pdf"         # BRAID/NEST partner    (unique size, 24 pp)

SCENARIOS = ["SEQF", "NSQF", "MISS", "NEST", "BRAID"]
N_VALUES = list(range(2, 21))      # 2..20
IMAGE_SIZE = 64 << 20              # 64 MiB
BASE_SEED = 0xC0FFEE

GAP_MIN, GAP_MAX = 2, 16           # inter-fragment / inter-file gap, in sectors


# --------------------------------------------------------------------------- #
# filler pool: real unrelated bytes (never zeros), tiled to any length
# --------------------------------------------------------------------------- #
def build_filler_pool(pdf_paths, want_bytes):
    chunks, total, i = [], 0, 0
    while total < want_bytes:
        b = open(pdf_paths[i % len(pdf_paths)], "rb").read()
        b = b if i % 2 == 0 else b[::-1]
        chunks.append(b)
        total += len(b)
        i += 1
    return (b"".join(chunks))[:max(want_bytes, SECTOR)]


def filler(pool, rng, n):
    if n <= 0:
        return b""
    start = rng.randrange(0, len(pool))
    out = (pool[start:] + pool) * (n // len(pool) + 1)
    return out[:n]


# --------------------------------------------------------------------------- #
# split a file into N sector-aligned, byte-exact fragments
# --------------------------------------------------------------------------- #
def split_ranges(L, N):
    """N (pdf_start, pdf_end) ranges; internal boundaries are multiples of SECTOR,
    strictly increasing, each fragment >= 1 sector; last ends at L."""
    bounds, prev = [0], 0
    for i in range(1, N):
        b = round(i * L / N / SECTOR) * SECTOR
        b = max(b, prev + SECTOR)
        bounds.append(b)
        prev = b
    bounds.append(L)
    if bounds[-2] >= L - SECTOR:
        raise ValueError("file of %d bytes too small for %d fragments" % (L, N))
    return [(bounds[i], bounds[i + 1]) for i in range(N)]


def non_identity_perm(N, rng):
    """A permutation of range(N) that is not the identity (a derangement-ish
    shuffle); for N==1 returns [0]."""
    if N == 1:
        return [0]
    order = list(range(N))
    for _ in range(20):
        rng.shuffle(order)
        if order != list(range(N)):
            return order
    return order[::-1]


# --------------------------------------------------------------------------- #
# image accumulator
# --------------------------------------------------------------------------- #
class ImageBuilder:
    def __init__(self, pool, rng):
        self.buf = bytearray()
        self.pool = pool
        self.rng = rng
        self.manifest = []     # per-PDF rows
        self.frags = []        # per-fragment rows

    def _pad(self):
        r = len(self.buf) % SECTOR
        if r:
            self.buf += filler(self.pool, self.rng, SECTOR - r)

    def gap(self, sectors=None):
        if sectors is None:
            sectors = self.rng.randint(GAP_MIN, GAP_MAX)
        self.buf += filler(self.pool, self.rng, sectors * SECTOR)

    def place_bytes(self, data):
        """Place a blob at the next sector boundary; return its disk offset."""
        self._pad()
        off = len(self.buf)
        self.buf += data
        return off

    # -- record helpers ----------------------------------------------------- #
    def add_pdf(self, name, full_bytes, role, num_fragments, recoverable, base):
        self.manifest.append(dict(
            fat_entry_name=name,
            sha256=hashlib.sha256(full_bytes).hexdigest(),
            pdf_size_bytes=len(full_bytes),
            scenario=None,        # filled by caller
            role=role,
            num_fragments=num_fragments,
            recoverable=recoverable,
            base_offset=base))

    def add_frag(self, name, role, nfrag, frag_index, disk_order,
                 present, p0, p1, disk_off):
        self.frags.append(dict(
            fat_entry_name=name, role=role, num_fragments=nfrag,
            frag_index=frag_index, disk_order=disk_order, present=present,
            pdf_start=p0, pdf_end=p1, length=p1 - p0, disk_offset=disk_off))


# --------------------------------------------------------------------------- #
# place a fragmented file according to a disk plan
# --------------------------------------------------------------------------- #
def place_fragmented(ib, name, data, role, ranges, disk_order, missing_idx=None,
                     nest_into=None):
    """ranges: pdf-order list of (p0,p1). disk_order: list of frag indices in the
    order they are written to disk. missing_idx: frag index overwritten by filler
    (MISS). nest_into: (frag_index_after_which, secondary_blob_fn) to drop a whole
    nested file into the gap following that disk position (NEST)."""
    N = len(ranges)
    base_first = None
    for pos, fi in enumerate(disk_order):
        p0, p1 = ranges[fi]
        if missing_idx is not None and fi == missing_idx:
            # fragment is GONE: write same-length unrelated data, record present=False
            ib.gap(sectors=max(1, (p1 - p0 + SECTOR - 1) // SECTOR))
            ib.add_frag(name, role, N, fi, pos, False, p0, p1, -1)
        else:
            off = ib.place_bytes(data[p0:p1])
            if fi == 0:
                base_first = off
            ib.add_frag(name, role, N, fi, pos, True, p0, p1, off)
        # gap after each fragment (except keep it tight for braid which supplies
        # the partner fragment as the "gap" itself -> handled by caller order)
        if nest_into is not None and nest_into[0] == pos:
            nest_into[1]()            # place the whole secondary here
        else:
            ib.gap()
    return base_first


# --------------------------------------------------------------------------- #
# build one image for (scenario, N)
# --------------------------------------------------------------------------- #
def build_one(scenario, N, pdfs, target_path, secondary_path, image_size, seed):
    rng = random.Random(seed)
    pool = build_filler_pool(pdfs, 8 << 20)
    ib = ImageBuilder(pool, rng)

    target = open(target_path, "rb").read()
    secondary = open(secondary_path, "rb").read()
    tname = os.path.basename(target_path)
    sname = os.path.basename(secondary_path)

    uses_secondary = scenario in ("NEST", "BRAID")
    distractors = [p for p in pdfs
                   if os.path.basename(p) != tname
                   and not (uses_secondary and os.path.basename(p) == sname)]
    rng.shuffle(distractors)
    half = len(distractors) // 2

    def place_distractors(lst):
        for p in lst:
            d = open(p, "rb").read()
            base = ib.place_bytes(d)
            ib.add_pdf(os.path.basename(p), d, "distractor", 1, True, base)
            ib.gap()

    # leading filler so nothing starts at offset 0
    ib.gap(sectors=rng.randint(1, 8))
    place_distractors(distractors[:half])

    tranges = split_ranges(len(target), N)

    if scenario == "SEQF":
        base = place_fragmented(ib, tname, target, "target", tranges, list(range(N)))
        ib.add_pdf(tname, target, "target", N, True, base)

    elif scenario == "NSQF":
        order = non_identity_perm(N, rng)
        base = place_fragmented(ib, tname, target, "target", tranges, order)
        ib.add_pdf(tname, target, "target", N, True, base)

    elif scenario == "MISS":
        miss = N // 2                       # a middle fragment
        base = place_fragmented(ib, tname, target, "target", tranges,
                                list(range(N)), missing_idx=miss)
        ib.add_pdf(tname, target, "target", N, False, base)  # not fully recoverable

    elif scenario == "NEST":
        nest_pos = N // 2 - 1 if N >= 2 else 0   # gap after this disk position
        holder = {}

        def drop_secondary():
            off = ib.place_bytes(secondary)
            holder["base"] = off
            # record the nested file's single-fragment geometry too
            ib.add_frag(sname, "secondary", 1, 0, 0, True, 0, len(secondary), off)

        base = place_fragmented(ib, tname, target, "target", tranges,
                                list(range(N)), nest_into=(nest_pos, drop_secondary))
        ib.add_pdf(tname, target, "target", N, True, base)
        ib.add_pdf(sname, secondary, "secondary", 1, True, holder["base"])

    elif scenario == "BRAID":
        sranges = split_ranges(len(secondary), N)
        tbase = sbase = None
        for i in range(N):                  # F1 G1 F2 G2 ... Fn Gn
            tp0, tp1 = tranges[i]
            off = ib.place_bytes(target[tp0:tp1])
            if i == 0:
                tbase = off
            ib.add_frag(tname, "target", N, i, 2 * i, True, tp0, tp1, off)
            sp0, sp1 = sranges[i]
            off2 = ib.place_bytes(secondary[sp0:sp1])
            if i == 0:
                sbase = off2
            ib.add_frag(sname, "secondary", N, i, 2 * i + 1, True, sp0, sp1, off2)
        ib.add_pdf(tname, target, "target", N, True, tbase)
        ib.add_pdf(sname, secondary, "secondary", N, True, sbase)
    else:
        raise ValueError(scenario)

    place_distractors(distractors[half:])

    # pad whole image to requested size with filler (never truncate real data)
    if len(ib.buf) > image_size:
        raise SystemExit("content %d > image_size %d (raise IMAGE_SIZE)"
                         % (len(ib.buf), image_size))
    ib.buf += filler(pool, rng, image_size - len(ib.buf))

    for row in ib.manifest:
        row["scenario"] = scenario
    return ib


def write_outputs(ib, out_base, scenario, N):
    with open(out_base + ".img", "wb") as fd:
        fd.write(ib.buf)
    mcols = ["fat_entry_name", "sha256", "pdf_size_bytes", "scenario", "role",
             "num_fragments", "recoverable", "base_offset"]
    with open(out_base + "_manifest.csv", "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=mcols)
        w.writeheader()
        for r in ib.manifest:
            w.writerow(r)
    fcols = ["fat_entry_name", "role", "num_fragments", "frag_index",
             "disk_order", "present", "pdf_start", "pdf_end", "length",
             "disk_offset"]
    with open(out_base + "_fragments.csv", "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=fcols)
        w.writeheader()
        for r in ib.frags:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# verification: rebuild each fragmented file from its image+geometry
# --------------------------------------------------------------------------- #
def verify_image(out_base, src_sha):
    img = open(out_base + ".img", "rb").read()
    rows = list(csv.DictReader(open(out_base + "_fragments.csv", newline="")))
    by_file = {}
    for r in rows:
        by_file.setdefault((r["fat_entry_name"], r["role"]), []).append(r)
    ok = True
    msgs = []
    # manifest tells us which targets are fully recoverable
    man = {(r["fat_entry_name"], r["role"]): r
           for r in csv.DictReader(open(out_base + "_manifest.csv", newline=""))}
    for (name, role), frs in by_file.items():
        present = [r for r in frs if r["present"] == "True"]
        missing = [r for r in frs if r["present"] == "False"]
        # reconstruct from present fragments in pdf order
        present.sort(key=lambda r: int(r["frag_index"]))
        blob = b"".join(img[int(r["disk_offset"]):int(r["disk_offset"]) + int(r["length"])]
                        for r in present)
        recov = man.get((name, role), {}).get("recoverable")
        if not missing:
            got = hashlib.sha256(blob).hexdigest()
            if got == src_sha.get(name):
                msgs.append("  OK    %-8s %-16s rebuilt %d frag -> sha match"
                            % (role, name, len(present)))
            else:
                ok = False
                msgs.append("  FAIL  %-8s %-16s sha MISMATCH" % (role, name))
        else:
            # MISS: present fragments must equal their byte ranges of the original
            orig = src_bytes.get(name)
            good = all(img[int(r["disk_offset"]):int(r["disk_offset"]) + int(r["length"])]
                       == orig[int(r["pdf_start"]):int(r["pdf_end"])] for r in present)
            if good and recov == "False":
                msgs.append("  OK    %-8s %-16s MISS: %d/%d frag present, gap intact"
                            % (role, name, len(present), len(frs)))
            else:
                ok = False
                msgs.append("  FAIL  %-8s %-16s MISS reconstruction wrong" % (role, name))
    return ok, msgs


src_bytes = {}
src_sha = {}


def load_src(pdfs):
    for p in pdfs:
        b = open(p, "rb").read()
        src_bytes[os.path.basename(p)] = b
        src_sha[os.path.basename(p)] = hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
def jobs_for(mode):
    if mode == "demo":
        return [("SEQF", 2), ("SEQF", 3), ("BRAID", 2), ("BRAID", 3),
                ("NSQF", 4), ("MISS", 4), ("NEST", 4)]
    return [(s, n) for s in SCENARIOS for n in N_VALUES]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                    help="folder containing source PDFs (needs Linearized.pdf and "
                         "1.0.pdf, or override with --target/--secondary)")
    ap.add_argument("--out", required=True, help="output folder for disk images")
    ap.add_argument("--target", default=TARGET_NAME)
    ap.add_argument("--secondary", default=SECONDARY_NAME)
    ap.add_argument("--size-mb", type=int, default=IMAGE_SIZE >> 20)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="verify existing images instead of (re)building")
    args = ap.parse_args()

    pdfs = sorted(glob.glob(os.path.join(args.src, "*.pdf")))
    if not pdfs:
        raise SystemExit("no PDFs in " + args.src)
    load_src(pdfs)
    target_path = os.path.join(args.src, args.target)
    secondary_path = os.path.join(args.src, args.secondary)
    image_size = args.size_mb << 20

    mode = "demo" if args.demo else "full"
    out_dir = os.path.join(args.out, "_demo") if args.demo else args.out
    os.makedirs(out_dir, exist_ok=True)
    jobs = jobs_for(mode)

    if args.verify:
        npass = 0
        for scenario, N in jobs:
            base = os.path.join(out_dir, "%s_f%02d" % (scenario, N))
            if not os.path.exists(base + ".img"):
                continue
            ok, msgs = verify_image(base, src_sha)
            npass += ok
            print("[%s] %s_f%02d" % ("PASS" if ok else "FAIL", scenario, N))
            for m in msgs:
                print(m)
        print("\n%d/%d images verified clean." % (npass, len(jobs)))
        return

    index = []
    for k, (scenario, N) in enumerate(jobs):
        seed = BASE_SEED + SCENARIOS.index(scenario) * 1000 + N
        ib = build_one(scenario, N, pdfs, target_path, secondary_path,
                       image_size, seed)
        base = os.path.join(out_dir, "%s_f%02d" % (scenario, N))
        write_outputs(ib, base, scenario, N)
        tgt_row = next(r for r in ib.manifest if r["role"] == "target")
        sec_row = next((r for r in ib.manifest if r["role"] == "secondary"), None)
        index.append(dict(
            image="%s_f%02d.img" % (scenario, N), scenario=scenario,
            num_fragments=N, image_size_bytes=len(ib.buf),
            target=tgt_row["fat_entry_name"], target_sha256=tgt_row["sha256"],
            target_recoverable=tgt_row["recoverable"],
            secondary=(sec_row["fat_entry_name"] if sec_row else ""),
            secondary_sha256=(sec_row["sha256"] if sec_row else "")))
        print("[%3d/%3d] %-6s N=%2d  %5.1fMB  frags=%d  -> %s_f%02d.img"
              % (k + 1, len(jobs), scenario, N, len(ib.buf) / (1 << 20),
                 sum(1 for r in ib.frags if r["present"] == True or r["present"]),
                 scenario, N))

    icols = ["image", "scenario", "num_fragments", "image_size_bytes", "target",
             "target_sha256", "target_recoverable", "secondary", "secondary_sha256"]
    with open(os.path.join(out_dir, "INDEX.csv"), "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=icols)
        w.writeheader()
        for r in index:
            w.writerow(r)
    print("\nWrote %d images + manifests to %s" % (len(jobs), out_dir))
    print("INDEX.csv written. Run:  py make_frag_corpus.py --verify %s"
          % ("--demo" if args.demo else "--full"))


if __name__ == "__main__":
    main()
