#!/usr/bin/env python3
"""
score_c2_ablation.py -- the C2 repair ablation table (repair OFF vs ON).

Scores SCAR's C2 output twice against the same ground truth:
  OFF : results/c2_ablation_norepair/<img>/scar/...   (carve.py --no-repair)
  ON  : results/full_carved_output/<img>/scar/...      (default, repair on)

For each gap-size image it reports Faithful_OFF, Faithful_ON, and the C2 gain
(the documents recovered only because bi-fragment repair was enabled). Uses the
same FAITHFUL definition as score_corpus.py (exact SHA-256 or full per-page
render-hash).

Usage:
  py score_c2_ablation.py ^
     --on   D:\\repo\\SCAR\\results\\full_carved_output ^
     --off  D:\\repo\\SCAR\\results\\c2_ablation_norepair ^
     --dataset D:\\repo\\SCAR\\dataset ^
     --ground-truth D:\\repo\\SCAR\\ground_truth ^
     --out c2_repair_ablation.csv
"""
import argparse
import csv
import os

import score_corpus as sc

# (image_id, short gap label)
C2 = [("C2-BIFRAG-gap1", "gap1"), ("C2-BIFRAG-gap8", "gap8"),
      ("C2-BIFRAG-gap64", "gap64"), ("C2-BIFRAG-gap512", "gap512"),
      ("C2-CONTROL-64MB", "control")]


def faithful_count(results_root, image_id, dataset, gt_dir, layout):
    """layout='nested' -> <image>/scar/fragmentcarve (full_carved_output);
    layout='flat'   -> <image>/fragmentcarve (a bare carve.py --out folder)."""
    image_dir = os.path.join(results_root, image_id)
    if not os.path.isdir(image_dir):
        return None, None
    targets, distractors = sc.load_manifest_candidates(image_id, dataset, gt_dir)
    gt_entries = sc.build_gt_entries(targets)
    dist_entries = sc.build_gt_entries(distractors) if distractors else []
    if layout == "flat":
        import glob
        paths = glob.glob(os.path.join(image_dir, "fragmentcarve", "*.pdf"))
    else:
        paths = sc.list_tool_candidates(image_dir, "scar")
    f, p, dup, fa, junk, gt_docs = sc.score_tool_bytes_mode(gt_entries, paths, dist_entries)
    return f, gt_docs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--on", required=True, help="results root WITH repair (full_carved_output)")
    ap.add_argument("--off", required=True, help="results root WITHOUT repair (ablation)")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    gt_dir = os.path.join(args.ground_truth, "C2 pdfs")
    rows = []
    for image_id, gap in C2:
        f_off, gt = faithful_count(args.off, image_id, args.dataset, gt_dir, "flat")
        f_on, _ = faithful_count(args.on, image_id, args.dataset, gt_dir, "nested")
        if f_off is None or f_on is None:
            print("skip %s (missing OFF or ON output)" % image_id)
            continue
        gain = f_on - f_off
        rows.append(dict(
            Gap=gap, GT_docs=gt,
            Faithful_OFF=f_off, Faithful_ON=f_on, C2_gain=gain,
            Recall_OFF_pct=round(f_off / gt * 100.0, 1) if gt else 0.0,
            Recall_ON_pct=round(f_on / gt * 100.0, 1) if gt else 0.0,
        ))
        print("%-8s GT=%d  OFF=%d  ON=%d  gain=%+d" % (gap, gt, f_off, f_on, gain))

    with open(args.out, "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=["Gap", "GT_docs", "Faithful_OFF",
                                           "Faithful_ON", "C2_gain",
                                           "Recall_OFF_pct", "Recall_ON_pct"])
        w.writeheader()
        w.writerows(rows)
    print("\n-> %s" % args.out)


if __name__ == "__main__":
    main()
