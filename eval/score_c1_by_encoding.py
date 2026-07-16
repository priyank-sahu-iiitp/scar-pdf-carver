#!/usr/bin/env python3
"""
score_c1_by_encoding.py -- the C1 capability-gap table (per-document).

The main scorer (score_corpus.py) reports per-IMAGE counts. This one reports,
for the C1 corpus only, per-DOCUMENT recovery joined with each document's real
cross-reference encoding, so we can show the capability gap directly:

  a keyword/classic carver recovers ~0 of the "Cohen-blind" xref-stream PDFs,
  while SCAR (C1) recovers them.

Encoding label comes from src/generators/c1_classification.csv (produced by
classify_c1.py). Documents are grouped as:
  classic table (Cohen-readable)
  hybrid (Cohen-readable)
  xref-stream +PNG / +objstm / plain (Cohen-blind)

A document counts as recovered by a tool if that tool produced a carved file
matching it at tier FAITHFUL (exact SHA-256 or full per-page render-hash) --
same definition as score_corpus.py.

Output CSV: one row per (encoding_group, tool) with recovered / total / recall%.
Reuses score_corpus.py's rendering + matching so the definitions stay identical.

Usage:
  py score_c1_by_encoding.py ^
     --results D:\\repo\\SCAR\\results\\full_carved_output ^
     --dataset D:\\repo\\SCAR\\dataset ^
     --ground-truth D:\\repo\\SCAR\\ground_truth ^
     --class-csv D:\\repo\\SCAR\\src\\generators\\c1_classification.csv ^
     --out c1_capability_gap.csv
"""
import argparse
import csv
import glob
import os

import score_corpus as sc

C1_IMAGES = ("C1-CONT", "C1-BIFRAG", "C1-3FRAG")


def encoding_group(label):
    """Collapse the fine-grained classify_c1 label into the 3 paper groups."""
    l = (label or "").lower()
    if "cohen-blind" in l or "xref-stream" in l:
        return "xref-stream (Cohen-blind)"
    if "hybrid" in l:
        return "hybrid (Cohen-readable)"
    if "classic" in l:
        return "classic table (Cohen-readable)"
    return "unknown"


def load_encodings(class_csv):
    enc = {}
    with open(class_csv, newline='') as fd:
        for row in csv.DictReader(fd):
            enc[row["file"]] = encoding_group(row.get("label"))
    return enc


def precompute_candidates(tool_paths):
    """Render/hash every candidate ONCE. Returns list of (sha, page_hashes|None).
    This avoids re-rendering every candidate once per ground-truth document
    (the original O(docs x candidates) rendering cost)."""
    out = []
    for path in tool_paths:
        try:
            sha = sc.sha256_file(path)
        except OSError:
            continue
        sig = sc.render_signature(path)
        page_hashes = sig[0] if sig is not None else None
        out.append((sha, page_hashes))
    return out


def doc_recovered_faithful(gt_bytes, cand_cache):
    """FAITHFUL if any precomputed candidate matches this GT doc by exact
    SHA-256 or full per-page render-hash tuple."""
    gt_sha = sc.hashlib.sha256(gt_bytes).hexdigest()
    # render GT doc once
    gt_pages = None
    try:
        doc = sc.fitz.open(stream=gt_bytes, filetype="pdf")
        ph = [sc.hashlib.md5(p.get_pixmap(dpi=72).samples).hexdigest() for p in doc]
        doc.close()
        gt_pages = tuple(ph) if ph else None
    except Exception:
        gt_pages = None
    for sha, pages in cand_cache:
        if sha == gt_sha:
            return True
        if gt_pages is not None and pages is not None and pages == gt_pages:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--class-csv", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    enc = load_encodings(args.class_csv)
    gt_dir = os.path.join(args.ground_truth, "C1 pdfs")

    # tally[(group, tool)] = [recovered, total]
    tally = {}
    for image_id in C1_IMAGES:
        image_dir = os.path.join(args.results, image_id)
        if not os.path.isdir(image_dir):
            print("skip (no output): %s" % image_id)
            continue
        targets, _distractors = sc.load_manifest_candidates(image_id, args.dataset, gt_dir)
        for tool in sc.TOOLS:
            paths = sc.list_tool_candidates(image_dir, tool)
            cand_cache = precompute_candidates(paths)   # render candidates once
            for name, gt_bytes in targets.items():
                grp = enc.get(name, "unknown")
                ok = doc_recovered_faithful(gt_bytes, cand_cache)
                key = (grp, tool)
                cell = tally.setdefault(key, [0, 0])
                cell[0] += 1 if ok else 0
                cell[1] += 1
        print("scored %s" % image_id)

    group_order = ["classic table (Cohen-readable)",
                   "hybrid (Cohen-readable)",
                   "xref-stream (Cohen-blind)", "unknown"]
    tool_order = {t: i for i, t in enumerate(sc.TOOLS)}

    rows = []
    for grp in group_order:
        for tool in sc.TOOLS:
            if (grp, tool) not in tally:
                continue
            rec, tot = tally[(grp, tool)]
            rows.append(dict(Encoding=grp, Tool=tool, Recovered=rec, Total=tot,
                             Recall_pct=round(rec / tot * 100.0, 1) if tot else 0.0))

    with open(args.out, "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=["Encoding", "Tool", "Recovered", "Total", "Recall_pct"])
        w.writeheader()
        w.writerows(rows)

    for r in rows:
        print("%-30s %-10s %2d/%2d  %5.1f%%"
              % (r["Encoding"], r["Tool"], r["Recovered"], r["Total"], r["Recall_pct"]))
    print("\n-> %s" % args.out)


if __name__ == "__main__":
    main()
