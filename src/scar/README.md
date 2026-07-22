# SCAR — Semantic PDF Fragment Carver (CLI)

SCAR recovers PDF documents from raw disk images when the filesystem metadata
is gone. It extends Cohen's (2007) cross-reference–driven carving model with
three additions: recovery of PDF 1.5+ compressed cross-reference streams,
anchored repair of compressed streams that are split across a disk gap, and a
graded `CONFIRMED / PARTIAL / REJECT` validator.

This repository is the **command-line semantic engine only** — the GUI viewer
and the signature carver are not included, so every recovery is attributable to
the semantic pipeline.

---

## Requirements

- **Python 3.7+**
- Python packages (see `requirements.txt`):
  - `pyahocorasick` — single-pass keyword indexing
  - `pikepdf` — structural validation (qpdf binding)
  - `pymupdf` — page-render validation

```bash
pip install -r requirements.txt
```

On Windows, if `pyahocorasick` fails to build, install the Microsoft C++ Build
Tools first. On Debian/Ubuntu, `sudo apt install libqpdf-dev` helps `pikepdf`.

---

## Usage

```bash
python carve.py <disk-image> [--out DIR] [--quiet]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `disk-image` | yes | Raw disk image (`.dd`, `.img`, `.raw`, or any flat binary) |
| `--out DIR`  | no  | Output directory (default: `carved_pdfs`) |
| `--quiet`    | no  | Suppress the progress bar |
| `--no-repair` | no | Ablation: disable C2 anchored bi-fragment repair |
| `--no-xrefstream` | no | Ablation: disable C1 xref-stream / `/ObjStm` decoding |
| `--no-dedup` | no | Ablation: keep all raw reconstructions (no content dedup) |

Each run also writes `_work/evidence_hash.log` — the SHA-256 of the source
image taken before and after analysis, so a reviewer can confirm the carve
never altered the evidence.

Example:

```bash
python carve.py evidence.dd --out recovered
```

---

## Pipeline

| Stage | What it does |
|------:|--------------|
| 1 | **Structural index** — one Aho-Corasick pass builds `index.idx` |
| 2 | **Anchor derivation** — classic xref table · **xref-stream decode (C1)** · synthetic xref · header→EOF fallback |
| 2.5 | **`/ObjStm` direct carve (C1)** — documents whose objects live in compressed object streams |
| 3 | **Reconstruction** — Cohen generator/discriminator over the offset map |
| 3a | **Bi-fragment repair (C2)** — re-stitch a FlateDecode stream split across a disk gap |
| 4 | **Graded validation (E4)** — P1–P6 → `CONFIRMED / PARTIAL / REJECT` (pikepdf + pymupdf) |
| 5 | **Content dedup** — SHA-256 → render-hash → object-set containment → `UNIQUE` |

---

## Output

Recovered files are written under `--out`:

| Path | Meaning |
|------|---------|
| `fragmentcarve/*.pdf` | Recovered PDFs (semantic engine + repaired streams) |
| `dedup_results.csv` | Per-file verdict, dedup label, page count |
| `_work/` | Intermediates (maps, index, rejected/duplicate, repair log) |

The console prints a summary: the graded verdict counts
(`CONFIRMED / PARTIAL / REJECT`), the dedup breakdown, and the final
`UNIQUE` count.

---

## Module map

| File | Role |
|------|------|
| `carve.py` | CLI orchestrator (this entry point) |
| `pdf_carver.py` | Stage 1 index + Stage 2 anchor derivation |
| `PDF.py` | PDF parser + cross-reference-stream decoder (C1) |
| `Carver.py` | Offset-mapping reassembler |
| `PDFForcer.py` | Reconstruction (Cohen generator/discriminator) |
| `repair.py` | Anchored bi-fragment repair (C2) |
| `verifier.py` | Graded P1–P6 validator (C3) |
| `verify.py` | Lightweight inline validity check |
| `dedup.py` | Content-aware deduplication |
| `lexer.py` | Tokeniser used by the parser |

---

## License

Released under the **GPL-2.0-or-later** — see [LICENSE](../../LICENSE).
Derived from M. I. Cohen's DFRWS 2007 submission; see
[ATTRIBUTION.md](../../ATTRIBUTION.md) for full provenance.
