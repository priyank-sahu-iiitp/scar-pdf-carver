# SCAR — Semantic Carving of Modern PDFs

Reference implementation, evaluation corpus generators, and scoring scripts for:

> **SCAR: Semantic Carving of Modern PDFs with Cross-Reference-Stream Recovery
> and Anchored Bi-fragment Repair.** Priyank Kumar Sahu.

SCAR extends Cohen's (2007) cross-reference–driven semantic PDF carver with two
contributions, keeping his mapping-function core unchanged:

- **C1 — Cross-reference-stream recovery.** Decodes PDF 1.5+ `/Type /XRef`
  streams (FlateDecode inflation, PNG-predictor reversal, `/W`-width and
  `/Index`-range entry parsing) to recover identified points for documents that
  no longer carry a classic `xref` table.
- **C2 — Anchored bi-fragment repair.** Repairs an intra-object compressed
  stream split across two fragments by bounding the reassembly search to the
  single interval between two consecutive anchors and validating each candidate
  by zlib decompression completeness.

A set of engineering elements (E1–E6: single-pass index, direct-length fast
path, header-to-EOF fallback, independent render-level validation, structural
and content deduplication, over-read bound) make the tool fast and
evidentiarily defensible.

---

## Repository layout

```
scar-pdf-carver/
├── src/
│   ├── scar/            SCAR carver — CLI entry point: carve.py
│   ├── cohen/           Cohen (2007) reimplementation — entry: carve_pdf.py
│   ├── chen/            Chen et al. (2008) reimplementation — entry: chen_carve.py
│   └── generators/      Seeded corpus generators + xref-encoding classifier
├── ground_truth/        The 112 planted source PDFs, by corpus (folder names
│                        are load-bearing — the scorer resolves ground truth by them)
├── manifests/           Per-image ground-truth manifests (sector geometry per doc)
├── eval/
│   ├── *.py             Scoring + statistics scripts
│   └── results/         Pre-computed result tables (verify the paper's numbers here)
└── docs/
    └── baseline_carving_commands.txt   Exact foremost/scalpel/photorec commands
```

Two corpora are **not** shipped as files and are rebuilt/downloaded instead:

- The **127 synthetic disk images** (tens of GB) are regenerated
  byte-deterministically by `src/generators/` from `ground_truth/` (below).
- The **7 external images** are downloaded from their official sources: the six
  NIST CFReDS L0–L5 images (<https://cfreds.nist.gov>) and the DFRWS 2007
  challenge image (<https://dfrws.org>). DFRWS never released its source PDFs,
  only their MD5 digests, so that corpus is scored by whole-file MD5 alone.

---

## Requirements

- **Python 3.10+** (developed on 3.14)
- `pip install -r requirements.txt` → `pyahocorasick`, `pikepdf` (qpdf binding),
  `pymupdf` (MuPDF), `openpyxl`
- **foremost 1.5.7**, **scalpel 1.60**, **photorec 7.1** for the three
  signature-carver baselines (run under WSL/Linux — see
  `docs/baseline_carving_commands.txt`)

On Windows, if `pyahocorasick` fails to build, install the Microsoft C++ Build
Tools first. On Debian/Ubuntu, `sudo apt install libqpdf-dev` helps `pikepdf`.

> Throughout, `py` is the Windows Python launcher; on Linux/macOS use `python3`.

---

## Quick start — carve one image

```bash
py src/scar/carve.py path/to/disk.img --out recovered
```

Recovered PDFs land in `recovered/fragmentcarve/`; a per-file verdict/dedup
table in `recovered/dedup_results.csv`; all intermediates (maps, index, the
`evidence_hash.log`) in `recovered/_work/`. Nothing produced by a run is deleted
by the run — a second examiner can re-trace every reconstruction.

Ablation flags: `--no-repair` (disable C2), `--no-xrefstream` (disable C1),
`--no-dedup` (keep all raw reconstructions).

---

## Reproducing the paper, end to end

### Step 1 — Rebuild the 127 synthetic disk images

Each generator takes the corpus's source-PDF folder as `--src` and writes images
**and** their `<image>_manifest.csv` into `--out`:

```bash
py "src/generators/3 disk image.py"  --src "ground_truth/C1 pdfs"       --out dataset            #  3  C1 images
py "src/generators/5 disk image.py"  --src "ground_truth/C2 pdfs"       --out dataset --best5    #  5  C2 images
py "src/generators/24 disk image.py" --src "ground_truth/24 image pdfs" --out dataset            # 24  SYN-24 images
py "src/generators/95 disk image.py" --src "ground_truth/95 FRAG pdfs"  --out dataset --full     # 95  depth-sweep images
```

Generators are seeded, so this rebuilds the images byte-for-byte. The 95-sweep
generator has a `--verify` mode that reconstructs each fragmented target from its
manifest and checks the SHA-256 against the source. Then download the **7
external images** (NIST CFReDS L0–L5, DFRWS 2007) into the same `dataset/`
folder so all 134 images sit together.

### Step 2 — Run all six carvers on every image

Each tool writes into `results/full_carved_output/<image>/<tool>/`. For a single
image `disk.img`:

```bash
py src/scar/carve.py       dataset/disk.img --out results/full_carved_output/disk/scar
py src/cohen/carve_pdf.py  dataset/disk.img --out results/full_carved_output/disk/cohen
py src/chen/chen_carve.py  dataset/disk.img -o   results/full_carved_output/disk/chen
```

The three signature carvers run under WSL/Linux; the exact, unmodified commands
(including PhotoRec's two-pass default + paranoid runs) are in
`docs/baseline_carving_commands.txt`.

To sweep all 134 images (the three Python carvers shown; run the signature
carvers per `docs/baseline_carving_commands.txt`):

```bash
for img in dataset/*.img dataset/*.dd; do
  name=$(basename "$img"); name="${name%.*}"
  out="results/full_carved_output/$name"
  py src/scar/carve.py      "$img" --out "$out/scar"
  py src/cohen/carve_pdf.py "$img" --out "$out/cohen"
  py src/chen/chen_carve.py "$img" -o   "$out/chen"
done
```

**For the C2 ablation (Table 4)** you also need a repair-disabled SCAR run on the
C2 images into a separate tree:

```bash
for img in dataset/C2-*.img; do
  name=$(basename "$img" .img)
  py src/scar/carve.py "$img" --no-repair --out "results/ablation_off/$name/scar"
done
```

### Step 3 — Score every tool with one content-level procedure

Scoring is a three-tier content cascade — exact file hash, then per-page render
hash (MuPDF, 72 dpi), then extracted-text hash — applied identically to all six
tools. `--dataset` points at the folder holding the images **and** their
manifests (i.e. the `dataset/` you regenerated into).

```bash
# Per-image recall / false-accept for all six tools  (feeds Tables 3, 5, 6)
py eval/score_corpus.py \
    --results results/full_carved_output \
    --dataset dataset \
    --ground-truth ground_truth \
    --out eval/results/scored_results_render_hash.csv
```

Scores were computed on Windows with the default text encoding. Under UTF-8 (e.g. Linux), one file in BRAID_f14 renders instead of failing, so PhotoRec on that image scores Junk 4, FA 4 rather than Junk 8, FA 0. No figure in the paper changes.

### Step 4 — Contribution tables and paired statistics

```bash
# Table 2 — C1 capability gap by cross-reference encoding
py eval/score_c1_by_encoding.py \
    --results results/full_carved_output \
    --dataset dataset \
    --ground-truth ground_truth \
    --class-csv "src/generators/c1_classification.csv" \
    --out eval/results/c1_capability_gap.csv

# Table 4 — repair-stage ablation (uses the --no-repair tree from Step 2)
py eval/score_c2_ablation.py \
    --on  results/full_carved_output \
    --off results/ablation_off \
    --dataset dataset \
    --ground-truth ground_truth \
    --out eval/results/c2_repair_ablation.csv

# Per-document paired outcomes (input to the McNemar test)
py eval/score_per_document.py \
    --results results/full_carved_output \
    --dataset dataset \
    --ground-truth ground_truth \
    --out eval/results/per_document_recovery.csv

# Table 7 — exact two-sided McNemar vs each baseline
py eval/mcnemar.py \
    --in  eval/results/per_document_recovery.csv \
    --out eval/results/mcnemar_results.csv
```

`eval/groundtruth.py` is the shared three-tier matcher the scorers call; it can
also be run standalone on one carved folder:

```bash
py eval/groundtruth.py \
    --carved-dir results/full_carved_output/C1-CONT/scar/fragmentcarve \
    --src        "ground_truth/C1 pdfs" \
    --manifest   dataset/C1-CONT_manifest.csv \
    --out        C1-CONT_scar_scored.csv
```

### Script → output reference

| Script | Command flags | Produces |
|---|---|---|
| `src/scar/carve.py` | `<image> --out DIR [--no-repair] [--no-xrefstream] [--no-dedup]` | recovered PDFs + `_work/` audit trail |
| `src/cohen/carve_pdf.py` | `<image> --out DIR` | Cohen baseline recoveries |
| `src/chen/chen_carve.py` | `<image> -o DIR` | Chen et al. baseline recoveries |
| `eval/score_corpus.py` | `--results --dataset --ground-truth --out` | per-image recall / false-accept, all tools (Tables 3, 5, 6) |
| `eval/score_c1_by_encoding.py` | `--results --dataset --ground-truth --class-csv --out` | Table 2 |
| `eval/score_c2_ablation.py` | `--on --off --dataset --ground-truth --out` | Table 4 |
| `eval/score_per_document.py` | `--results --dataset --ground-truth --out` | per-document paired outcomes |
| `eval/mcnemar.py` | `--in --out` | Table 7 |
| `eval/groundtruth.py` | `--carved-dir --src [--manifest] --out` | three-tier content match for one folder |

Pre-computed outputs of all of the above are in `eval/results/`, so the paper's
numbers can be checked without a full re-run. Run any script with `--help` for
its full option list.

---

## Forensic soundness

The carver opens the source image strictly read-only in every code path. Each
run computes the SHA-256 of the image before and after analysis and appends both
digests, with timestamps, to `_work/evidence_hash.log`, so the examiner has a
checkable record that analysis did not alter the evidence. Every recovered byte
range is traceable through its map file to the exact image offset that supplied
it. Files that fail validation are kept and labelled suspect, never deleted.
These mechanisms support chain of custody; they do not by themselves constitute
it.

---
## License and attribution

**GPL-2.0-only** — see [LICENSE](LICENSE).

Both `src/scar/` and `src/cohen/` derive from Michael I. Cohen's DFRWS 2007
Forensic Challenge submission ("Advanced carving techniques", *Digital
Investigation* 4(3–4):119–128, 2007), which is distributed under the GNU
General Public License v2 or later. Exercising the option allowed by that 
upstream license, this repository and our own additions are distributed strictly 
under the terms of the **GNU General Public License v2.0 only**. Full 
provenance — what was derived, what was changed, and the original copyright
notices — is recorded in [ATTRIBUTION.md](ATTRIBUTION.md).

The planted PDFs in `ground_truth/` are the author's own Microsoft Word
documents, freely-licensed Overleaf templates, and files from the
research-redistributable GovDocs/Digital Corpora collection (Garfinkel et al.,
2009).
