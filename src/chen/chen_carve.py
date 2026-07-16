"""
chen_carve.py -- Standalone Chen-2008 cascade runner for any disk image.

Self-contained: imports the sibling ``chen_2008.py`` from the same folder,
so this script + chen_2008.py can be dropped into any directory and run.

Scans an image for %PDF- headers, then runs the five-algorithm cascade
from Chen et al. (2008) on each candidate region. Writes every renderable
result to an output directory and prints a summary table.

Usage
-----
    py chen_carve.py <image>  [-o OUTDIR]
                              [--max-chunk MB]
                              [--scan-step MB]

    <image>       Path to a raw disk image (.dd, .img, .raw, .iso, ...)
                  or any file containing embedded PDFs.
    -o OUTDIR     Where to write recovered PDFs and summary.json
                  (default: ./chen_recovered).
    --max-chunk   Maximum bytes per candidate region in MiB (default 64).
                  Each %PDF- header is followed by up to max-chunk bytes,
                  capped at the next header.
    --scan-step   Memory-map scan window in MiB (default 256). Set lower
                  for very large images on low-RAM machines.

Examples
--------
    py chen_carve.py disk.dd
    py chen_carve.py "C:\\path\\to\\case42.img" -o out\\case42
"""

from __future__ import annotations

import argparse
import json
import mmap
import re
import sys
import time
from pathlib import Path
from typing import Iterator, List, Tuple

# Import the sibling chen_2008.py from this same folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chen_2008 import run_all_algorithms   # noqa: E402

_HEADER_RE = re.compile(rb"%PDF-\d+\.\d+")


def scan_headers(mm, scan_step: int) -> Iterator[int]:
    """Yield absolute byte offsets of every %PDF- header in `mm`."""
    pos = 0
    n = len(mm)
    while pos < n:
        end = min(pos + scan_step, n)
        for m in _HEADER_RE.finditer(mm, pos, end):
            yield m.start()
        pos = end - 16 if end < n else n


def plan_regions(
    headers: List[int], image_size: int, max_chunk: int
) -> List[Tuple[int, int]]:
    """Turn header offsets into (start, end) regions capped at max_chunk."""
    regions: List[Tuple[int, int]] = []
    for i, h in enumerate(headers):
        nxt = headers[i + 1] if i + 1 < len(headers) else image_size
        end = min(h + max_chunk, nxt, image_size)
        regions.append((h, end))
    return regions


def main() -> int:
    ap = argparse.ArgumentParser(description="Chen-2008 cascade carver")
    ap.add_argument("image", type=Path, help="raw disk image")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("chen_recovered"),
                    help="output directory (default: ./chen_recovered)")
    ap.add_argument("--max-chunk", type=float, default=64.0,
                    help="max MiB per candidate region (default 64)")
    ap.add_argument("--scan-step", type=float, default=256.0,
                    help="header-scan window in MiB (default 256)")
    args = ap.parse_args()

    if not args.image.is_file():
        print(f"error: {args.image} not found", file=sys.stderr)
        return 2

    args.outdir.mkdir(parents=True, exist_ok=True)
    max_chunk = int(args.max_chunk * 1024 * 1024)
    scan_step = int(args.scan_step * 1024 * 1024)

    image_size = args.image.stat().st_size
    print(f"image      : {args.image}  ({image_size / 1e6:.1f} MB)")
    print(f"out dir    : {args.outdir}")
    print(f"max chunk  : {max_chunk / 1e6:.0f} MB")
    print(f"scan step  : {scan_step / 1e6:.0f} MB")

    t0 = time.perf_counter()
    summary: List[dict] = []

    with args.image.open("rb") as f:
        try:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            using_mmap = True
        except (OSError, ValueError) as e:
            print(f"mmap failed ({e}); loading into RAM...", file=sys.stderr)
            mm = f.read()
            using_mmap = False

        headers = sorted(set(scan_headers(mm, scan_step)))
        print(f"headers    : {len(headers)} %PDF- signatures found\n")
        if not headers:
            print("no PDF headers found; nothing to do.")
            if using_mmap:
                mm.close()
            return 0

        regions = plan_regions(headers, image_size, max_chunk)

        print(f"{'#':>3}  {'offset':>12}  {'size':>10}  {'pages':>5}  "
              f"{'objs':>5}  {'alg':<6}  notes")
        print("-" * 110)

        for i, (start, end) in enumerate(regions):
            raw = bytes(mm[start:end])
            res = run_all_algorithms(raw)
            pc = res["A5_PC"]

            saved = ""
            if pc.renderable and pc.pdf_bytes:
                out_path = args.outdir / f"r{i:04d}_off{start:012d}.pdf"
                out_path.write_bytes(pc.pdf_bytes)
                saved = out_path.name

            note = pc.notes.split(";")[0][:60]
            alg = "HFV" if "cascade=HFV" in pc.notes else (
                "ISV" if "cascade=ISV" in pc.notes else "FAIL"
            )
            print(f"{i:>3}  {start:>12}  {(end - start):>10}  "
                  f"{(pc.page_count or 0):>5}  {pc.n_objects:>5}  "
                  f"{alg:<6}  {note}")

            summary.append({
                "region":      i,
                "offset":      start,
                "size":        end - start,
                "renderable":  pc.renderable,
                "pages":       pc.page_count,
                "n_objects":   pc.n_objects,
                "cascade":     alg,
                "elapsed_s":   round(pc.elapsed_s, 4),
                "notes":       pc.notes,
                "saved":       saved or None,
                "per_alg":     {
                    k: {"renderable": v.renderable,
                        "n_obj":      v.n_objects,
                        "elapsed_s":  round(v.elapsed_s, 4),
                        "notes":      v.notes}
                    for k, v in res.items()
                },
            })

        if using_mmap:
            mm.close()

    elapsed = time.perf_counter() - t0
    n_ok = sum(1 for r in summary if r["renderable"])
    print("-" * 110)
    print(f"recovered  : {n_ok}/{len(summary)} PDFs in {elapsed:.2f}s")

    (args.outdir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"summary    : {args.outdir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
