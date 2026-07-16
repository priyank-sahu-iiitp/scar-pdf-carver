#!/usr/bin/env python3
"""Carve PDFs from a raw disk image.

Usage:
  python carve_pdf.py disk.dd --out recovered/
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_PY = sys.executable


def _run(cmd, cwd, label):
    print(f"[{label}]", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout, end="")
    if r.returncode != 0:
        print(r.stderr, end="", file=sys.stderr)
        sys.exit(f"FAILED at: {label}")
    return r


def main():
    ap = argparse.ArgumentParser(description="Carve PDFs from a raw disk image.")
    ap.add_argument("disk", help="Disk image (.dd / raw)")
    ap.add_argument("--out", default="carved_pdfs", help="Output directory")
    args = ap.parse_args()

    disk = os.path.abspath(args.disk)
    out = os.path.abspath(args.out)

    if not os.path.exists(disk):
        sys.exit(f"ERROR: not found: {disk}")

    os.makedirs(out, exist_ok=True)

    idx = os.path.join(out, "index.idx")

    _run([_PY, _HERE / "pdf_carver.py", "-i", idx, "-c", disk], out, "1/3 build index")
    _run([_PY, _HERE / "pdf_carver.py", "-i", idx, "-m", disk], out, "2/3 generate maps")

    maps = list(Path(out).glob("*.map"))
    if not maps:
        sys.exit("No .map files generated — no recoverable PDFs found.")

    ok, failed = 0, 0
    for m in sorted(maps):
        pdf = m.with_suffix(".pdf")
        r = subprocess.run(
            [_PY, _HERE / "PDFForcer.py", "-m", m.name, "-o", pdf.name, disk],
            cwd=out, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  SKIP {m.name}: carver exited {r.returncode}", file=sys.stderr)
            failed += 1
        else:
            size = pdf.stat().st_size if pdf.exists() else 0
            print(f"  {pdf.name}  {size} bytes")
            ok += 1

    print(f"\nDone. {ok} PDF(s) recovered, {failed} skipped → {out}/")


if __name__ == "__main__":
    main()
