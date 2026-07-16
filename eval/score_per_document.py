#!/usr/bin/env python3
"""
score_per_document.py -- per-document paired recovery outcomes, for McNemar.

The aggregate scorer (score_corpus.py) emits per-image counts, which cannot
support a paired significance test. This one emits, for every planted target
document, whether each tool recovered it FAITHFULLY (exact SHA-256 or full
per-page render-hash match) -- the same definition score_corpus.py uses. The
output is one row per (corpus, image, document, tool) with a 0/1 recovered
flag, which mcnemar.py then pairs by (corpus, image, document).

Reuses score_corpus.py so the matching definitions are identical. Candidates
for a given (image, tool) are rendered once and reused across that image's
documents. Runs one image per worker process.

Usage:
  py score_per_document.py --results ...\\full_carved_output --dataset ...\\dataset ^
     --ground-truth ...\\ground_truth --workers 20 --out per_document_recovery.csv
"""
import argparse
import csv
import glob
import hashlib
import os

import fitz
import score_corpus as sc


def gt_render_sig(gt_bytes):
    """(sha256, page_hash_tuple|None) for one ground-truth document."""
    sha = hashlib.sha256(gt_bytes).hexdigest()
    try:
        doc = fitz.open(stream=gt_bytes, filetype="pdf")
        ph = [hashlib.md5(p.get_pixmap(dpi=72).samples).hexdigest() for p in doc]
        doc.close()
        return sha, (tuple(ph) if ph else None)
    except Exception:
        return sha, None


def cand_sigs(paths):
    out = []
    for p in paths:
        try:
            s = sc.sha256_file(p)
        except OSError:
            continue
        sig = sc.render_signature(p)
        out.append((s, sig[0] if sig is not None else None))
    return out


def faithful(gt_sha, gt_pages, cands):
    for csha, cpages in cands:
        if csha == gt_sha:
            return True
        if gt_pages is not None and cpages is not None and cpages == gt_pages:
            return True
    return False


def score_one_image(task):
    image_dir = task["image_dir"]
    dataset = task["dataset"]
    gtroot = task["gtroot"]
    image_id = os.path.basename(image_dir)
    mode, gt_subdir = sc.classify_image(image_id)
    if mode is None:
        return []
    gt_dir = os.path.join(gtroot, gt_subdir)
    rows = []

    if mode in ("manifest", "folder"):
        if mode == "manifest":
            targets, _distractors = sc.load_manifest_candidates(image_id, dataset, gt_dir)
        else:
            targets = sc.load_folder_candidates(gt_dir)
        gt = {name: gt_render_sig(b) for name, b in targets.items()}
        for tool in sc.TOOLS:
            cands = cand_sigs(sc.list_tool_candidates(image_dir, tool))
            for name, (gsha, gpages) in gt.items():
                ok = faithful(gsha, gpages, cands)
                rows.append((sc_corpus(image_id), image_id, name, tool, 1 if ok else 0))
    elif mode == "md5list":
        entries = sc.load_md5list(gt_dir)  # (realname, size, md5)
        for tool in sc.TOOLS:
            paths = sc.list_tool_candidates(image_dir, tool)
            cand_md5 = set()
            for p in paths:
                try:
                    cand_md5.add(sc.md5_file(p))
                except OSError:
                    pass
            for realname, _size, md5 in entries:
                ok = md5 in cand_md5
                rows.append(("DFRWS", image_id, realname, tool, 1 if ok else 0))
    return rows


def sc_corpus(image_id):
    import re
    for pat, name in [(r'^C1-', 'C1'), (r'^C2-', 'C2'), (r'^SYN-', 'SYN-24'),
                      (r'^(SEQF|NSQF|MISS|NEST|BRAID)_f\d+$', '95-set'),
                      (r'^L\d_Documents$', 'L-series'),
                      (r'^dfrws-2007-challenge$', 'DFRWS')]:
        if re.match(pat, image_id):
            return name
    return "OTHER"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    image_dirs = sorted(d for d in glob.glob(os.path.join(args.results, "*"))
                        if os.path.isdir(d))
    tasks = [dict(image_dir=d, dataset=args.dataset, gtroot=args.ground_truth)
             for d in image_dirs]

    workers = max(1, (os.cpu_count() or 2) - 1) if args.workers == 0 else max(1, args.workers)
    workers = min(workers, len(tasks))

    all_rows = []
    if workers <= 1:
        for t in tasks:
            all_rows.extend(score_one_image(t))
    else:
        import concurrent.futures as cf
        with cf.ProcessPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(score_one_image, tasks):
                all_rows.extend(res)

    with open(args.out, "w", newline="") as fd:
        w = csv.writer(fd)
        w.writerow(["Corpus", "Image", "Document", "Tool", "Recovered"])
        w.writerows(all_rows)
    print("wrote %d rows -> %s" % (len(all_rows), args.out))


if __name__ == "__main__":
    main()
