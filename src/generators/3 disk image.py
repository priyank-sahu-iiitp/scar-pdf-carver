# -*- coding: utf-8 -*-
"""
Build the 3 C1-contribution disk images. Every PDF in --src is a target; each
image applies one fragmentation condition:

  C1-CONT    : every PDF contiguous            (capability gap: Cohen 0 on stream)
  C1-BIFRAG  : every PDF split into 2 fragments (signature carvers collapse)
  C1-3FRAG   : every PDF split into 3 fragments (SCAR degrades gracefully)

128 MB raw image, random-noise background, no filesystem (same convention as the
other corpora). Noise is drawn from Python's seeded `random` module (SEED=42),
so re-running with the same --src PDFs reproduces byte-identical images.
Manifest records each PDF's cross-reference encoding (from --class-csv, if
given), fragment count, sectors, sha256.

Usage:
  py "3 disk image.py" --src <pdf-folder> --out <dir>
"""
import argparse
import csv
import hashlib
import math
import mmap
import os
import random

SEC = 512
BLK = 4096
SPB = BLK // SEC
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLASS = os.path.join(_SCRIPT_DIR, "recomputed", "c1_classification.csv")
if not os.path.exists(CLASS):
    CLASS = os.path.join(_SCRIPT_DIR, "c1_classification.csv")
SIZE_MB = 128
GAP_BLK = 4
SEED = 42
IMAGES = [("C1-CONT", 1), ("C1-BIFRAG", 2), ("C1-3FRAG", 3)]


def _splitN(n, parts):
    parts = max(1, min(parts, n))
    base, rem = divmod(n, parts)
    return [base + (1 if i < rem else 0) for i in range(parts)]


class RawDisk:
    def __init__(self, path, size_mb):
        self.size = size_mb * 1024 * 1024
        self.tot_blk = self.size // BLK
        with open(path, 'wb') as f:
            w = 0
            while w < self.size:
                k = min(4 * 1024 * 1024, self.size - w)
                f.write(random.randbytes(k))
                w += k
        self._f = open(path, 'r+b')
        self._mm = mmap.mmap(self._f.fileno(), 0)
        self._ptr = 0

    def _sec(self, b):
        return b * SPB

    def _wblk(self, b, data):
        off = b * BLK
        if off + BLK <= self.size:
            self._mm[off:off + BLK] = (data + bytes(BLK))[:BLK]

    def _alloc(self):
        b = self._ptr
        self._ptr += 1
        return b

    def _noise(self, n):
        for _ in range(n):
            self._wblk(self._alloc(), random.randbytes(BLK))

    def write_frag(self, chunk):
        nb = max(1, math.ceil(len(chunk) / BLK))
        blks = [self._alloc() for _ in range(nb)]
        for i, b in enumerate(blks):
            self._wblk(b, chunk[i * BLK:(i + 1) * BLK])
        return (self._sec(blks[0]), self._sec(blks[-1]) + SPB - 1)

    def place(self, data, n_frags):
        """Place a PDF as n_frags sequential fragments separated by noise."""
        nblk = max(1, math.ceil(len(data) / BLK))
        parts = _splitN(nblk, n_frags)
        secs = []
        off = 0
        for j, pblk in enumerate(parts):
            chunk = data[off * BLK:(off + pblk) * BLK]
            off += pblk
            secs.append(self.write_frag(chunk))
            if j < len(parts) - 1:
                self._noise(GAP_BLK)
        return secs

    def finalize(self):
        self._mm.flush()
        self._mm.close()
        self._f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                    help="folder containing source PDFs (30 PDFs in the original C1 corpus)")
    ap.add_argument("--out", required=True, help="output folder for disk images")
    ap.add_argument("--class-csv", default=CLASS,
                    help="c1_classification.csv (from classify_c1.py); "
                         "missing file just leaves encoding='?'")
    args = ap.parse_args()

    labels = {}
    if os.path.exists(args.class_csv):
        for r in csv.DictReader(open(args.class_csv)):
            labels[r["file"]] = r["label"]
    pdfs = sorted(p for p in os.listdir(args.src) if p.lower().endswith(".pdf"))
    if not pdfs:
        raise SystemExit("no PDFs in " + args.src)
    data = [(nm, open(os.path.join(args.src, nm), "rb").read()) for nm in pdfs]
    os.makedirs(args.out, exist_ok=True)

    for name, nfrag in IMAGES:
        random.seed(SEED)
        img = os.path.join(args.out, name + ".img")
        disk = RawDisk(img, SIZE_MB)
        rows = []
        for nm, d in data:
            secs = disk.place(d, nfrag)
            rows.append((nm, labels.get(nm, "?"), nfrag, len(d),
                         hashlib.sha256(d).hexdigest(),
                         min(s for s, e in secs), max(e for s, e in secs), secs))
            disk._noise(random.randint(2, 10))
        disk.finalize()
        man = os.path.join(args.out, name + "_manifest.csv")
        with open(man, "w", newline="") as fd:
            hdr = ["fat_entry_name", "encoding", "role", "recoverable",
                   "num_fragments", "pdf_size_bytes", "sha256",
                   "first_sector", "last_sector"]
            for i in range(1, 4):
                hdr += ["frag%d_start" % i, "frag%d_end" % i]
            w = csv.writer(fd)
            w.writerow(hdr)
            for nm, lab, nf, sz, sha, fs, ls, secs in rows:
                cells = [nm, lab, "target", "True", nf, sz, sha, fs, ls]
                for i in range(3):
                    if i < len(secs):
                        cells += [secs[i][0], secs[i][1]]
                    else:
                        cells += ["", ""]
                w.writerow(cells)
        print("built %s (%d MB, %d PDFs x %d frags)" % (name, SIZE_MB, len(data), nfrag))
    print("manifests + images in %s" % args.out)


if __name__ == "__main__":
    main()
