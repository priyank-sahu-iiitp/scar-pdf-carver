# Disk Image Generators

Four scripts build the raw (no-filesystem) disk images used for PDF-carving
evaluation. Every image is produced by seeding Python's `random` module and
never touches `os.urandom`, so **re-running a script with the same `--src`
PDFs produces byte-identical `.img` files** — verifiable with `sha256sum`.

All four take the same two required flags:

```
py "<script>.py" --src <folder-of-source-pdfs> --out <output-folder> [mode flag]
```

Neither flag has a default — omitting either fails immediately with a clear
`argparse` error instead of silently writing to a personal machine path.

## Scripts

| Script | Produces | Mode flag | Source PDFs needed |
|---|---|---|---|
| `3 disk image.py` | `C1-CONT`, `C1-BIFRAG`, `C1-3FRAG` (3 images, 128 MB each) | — (always builds all 3) | any folder of PDFs (30 in the original corpus) |
| `5 disk image.py` | C2 bifragment-repair corpus | `--demo` (2), `--full` (26 = 24 bifrag + 2 control) | any folder of PDFs |
| `24 disk image.py` | `SYN-*` corpus, 6 scenarios × 2 sizes × 2 frag-counts | — (always builds all 24) | **exactly 20** PDFs, each ≥500 KB |
| `95 disk image.py` | fragment-count sweep, 5 scenarios × N=2..20 | `--demo` (7), `--full` (95), `--verify` (reconstruct & sha256-check existing images) | folder containing `Linearized.pdf` + `1.0.pdf` (or override with `--target`/`--secondary`) |

**Total evaluation corpus: 3 + 26 + 24 + 95 = 148 images.**
`5 disk image.py` also has a legacy `--best5` mode (5 images); it is not part
of the 148-image count above because its docstring itself notes the seed is
"a guess, not guaranteed to hash-match the original files" — i.e. it is
deterministic going forward but not verified reproducible against any earlier
run. Use `--full` instead for anything reported in the paper.

## Examples

```
py "3 disk image.py"  --src F:\C1pdf                 --out D:\out
py "5 disk image.py"  --src C:\pdfs\expC2             --out D:\out --full
py "24 disk image.py" --src C:\pdfs\syn20             --out D:\out
py "95 disk image.py" --src "C:\pdfs\frag_src"        --out D:\out --full
py classify_c1.py     --src F:\C1pdf                  --out recomputed\c1_classification.csv
```

## Verifying reproducibility

```
sha256sum D:\out\*.img > mine.txt
diff mine.txt sha256sums.txt   # sha256sums.txt ships alongside the dataset
```

Identical `--src` PDFs + identical script = identical hashes, every time.

## `classify_c1.py` (not a generator)

Labels each PDF in a folder by its real cross-reference encoding (classic
table vs. xref-stream, Cohen-readable vs. Cohen-blind) via raw-byte
inspection — no ML, purely rule-based. Writes `c1_classification.csv`, which
`3 disk image.py --class-csv <path>` optionally reads to fill in the
`encoding` column of its manifest. Safe to skip; without it the column just
reads `?`.
