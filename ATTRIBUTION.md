# Attribution and provenance

This repository contains code derived from third-party work. This file records
what came from where, so the lineage of every component is explicit.

---

## Michael Cohen — DFRWS 2007 Forensic Challenge submission

Both `src/cohen/` and `src/scar/` derive from Michael Cohen's winning
submission to the **DFRWS 2007 Forensic Challenge**, published alongside:

> M. I. Cohen, "Advanced carving techniques," *Digital Investigation*,
> 4(3–4):119–128, 2007. doi:10.1016/j.diin.2007.10.001

Source obtained from the DFRWS 2007 challenge archive
(`challenge-submissions/michael.cohen/dfrws/`).

**Files derived from that submission** (`PDF.py`, `Carver.py`, `PDFForcer.py`,
`lexer.py`, `pdf_carver.py`):

| Directory | What it is |
|---|---|
| `src/cohen/` | Cohen's carver, ported to Python 3 and used unmodified in behaviour as the **baseline** in our evaluation. |
| `src/scar/`  | **SCAR** — Cohen's mapping-function core, extended with our contributions C1 (cross-reference-stream recovery) and C2 (anchored bi-fragment repair), plus engineering elements E1–E6. |

Original copyright, as recorded in the headers of that distribution:

```
Copyright 2004: Commonwealth of Australia
Copyright 2006: Michael Cohen
```

That code is distributed under the **GNU General Public License, version 2 or
later**. Accordingly, this repository as a whole is licensed **GPL-2.0-or-later**
(see [LICENSE](LICENSE)), and our own additions are contributed under the same
terms.


> **Note on the derived files.** In the original distribution the GPL v2 header
> appears on sibling files of the same package (e.g. `BasicFormats.py`,
> `class.c`, `class.h`) rather than on each of the five PDF-carver files
> individually. We treat the package as a single GPL-licensed work and license
> our derivative accordingly, which is the conservative reading.

### Nature of our changes

Per GPL v2 §2(a), the derived files carry notices stating that they were
changed. In summary:

- **Ported** from Python 2 to Python 3 (bytes/str handling, print, imports).
- **`src/scar/` only** — added compressed cross-reference-stream (`/Type /XRef`)
  decoding with PNG-predictor reversal (C1); anchored bi-fragment stream repair
  (`repair.py`, C2); an independent render-level validator (`verifier.py`);
  content-aware deduplication (`dedup.py`); a single-pass Aho–Corasick index;
  and an over-read bound.
- **`src/cohen/` only** — kept behaviourally faithful to the original; changes
  limited to the Python 3 port and I/O plumbing needed to run it over our
  corpus.

---

## Chen et al. (2008) baseline

`src/chen/` implements the validation cascade described in:

> M. Chen, N. Zheng, M. Xu, Y. Lou, X. Wang, "Validation algorithms based on
> content characters and internal structure: the PDF file carving method,"
> *2008 International Symposium on Information Science and Engineering*,
> pp. 168–172.

---

## Planted evaluation documents (`ground_truth/`)

- Documents authored by the repository author in Microsoft Word.
- Documents compiled from freely-licensed Overleaf templates.
- Files from the **GovDocs1 / Digital Corpora** collection, released for
  research use (Garfinkel et al., 2009).

The **DFRWS 2007** challenge image and the **NIST CFReDS** images are *not*
redistributed here; they are obtained from their official sources
(<https://dfrws.org>, <https://cfreds.nist.gov>). DFRWS never released the
challenge's source documents — only their MD5 digests — so that corpus is
scored by whole-file MD5 alone.
