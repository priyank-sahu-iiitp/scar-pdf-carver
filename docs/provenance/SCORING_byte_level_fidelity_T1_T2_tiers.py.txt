#!/usr/bin/env python3
"""
groundtruth.py -- T1/T2 ground-truth fidelity check for carved PDFs.

This is evaluation-only code. It is deliberately NOT part of src\\SCAR: it
depends on knowing which PDF was planted where (via a generator's
_manifest.csv and the original --src PDFs), which SCAR itself must never
have access to when running against real, unknown evidence.

For every carved PDF in --carved-dir, this script:
  1. Builds the candidate ground-truth set from --manifest (the PDFs the
     generator actually planted in this image) read from --src. If no
     manifest is given, every PDF in --src is a candidate (slower).
  2. T1 -- tries an exact SHA-256 match against each candidate.
  3. T2 -- if no exact match, finds the candidate with the longest common
     byte prefix (fast, chunked C-level slice comparison -- NOT difflib;
     difflib's algorithm can be pathologically slow on binary/compressed
     PDF content and also assumes scattered edits, which is the wrong model
     here), then measures the differing byte count and whether the
     divergence point falls near the end of the file (trailer/EOF/padding,
     the expected carving failure mode) or earlier in the body (content).

Output: one CSV row per carved file. Optionally merges an existing
dedup_results.csv (T3/T4, already computed by verifier.py/dedup.py) by
filename, so one command produces the full T1-T4 fidelity table.

Usage:
  py groundtruth.py --carved-dir out\\fragmentcarve --src <planted-pdfs-dir> \\
                     --manifest out\\..\\DEMO-BIFRAG-gap8_manifest.csv \\
                     --dedup-csv out\\dedup_results.csv \\
                     --out fidelity_report.csv
"""
import argparse
import csv
import glob
import hashlib
import os

# Ground truth is "near the end" if the differing region starts at or after
# this fraction of the candidate file's length -- a heuristic for
# trailer/EOF/padding-only divergence vs. a difference inside real content.
TAIL_FRACTION = 0.95

# Chunk size for the fast prefix/suffix scan: compare whole chunks with a
# single C-level bytes `==` first, and only fall back to a per-byte loop
# inside the one chunk where a mismatch was found. O(n) with a good constant
# factor; avoids difflib entirely (its Ratcliff/Obershelp algorithm can be
# pathologically slow on binary/compressed content and solves the wrong
# problem -- carving mismatches are truncation-point differences, not
# scattered edits).
CHUNK = 1 << 12

NAME_COLUMNS = ("fat_entry_name", "pdf_name")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def common_prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n:
        j = min(i + CHUNK, n)
        if a[i:j] == b[i:j]:
            i = j
            continue
        for k in range(i, j):
            if a[k] != b[k]:
                return k
        i = j
    return n


def common_suffix_len(a, b, max_len):
    """Length of the common suffix, capped at max_len (the residual length
    left after the common prefix has already been accounted for)."""
    la, lb = len(a), len(b)
    n = min(la, lb, max_len)
    i = 0
    while i < n:
        j = min(i + CHUNK, n)
        if a[la - j:la - i] == b[lb - j:lb - i]:
            i = j
            continue
        for k in range(i, j):
            if a[la - 1 - k] != b[lb - 1 - k]:
                return k
        i = j
    return n


def load_candidates(src_dir, manifest_path):
    """Return {name: bytes} for every candidate ground-truth PDF."""
    names = None
    if manifest_path and os.path.exists(manifest_path):
        names = set()
        with open(manifest_path, newline='') as fd:
            reader = csv.DictReader(fd)
            name_col = next((c for c in NAME_COLUMNS if c in (reader.fieldnames or [])), None)
            if name_col is None:
                names = None  # unrecognised schema -- fall back to all of --src
            else:
                for row in reader:
                    nm = row.get(name_col)
                    if nm:
                        names.add(nm)

    candidates = {}
    for path in sorted(glob.glob(os.path.join(src_dir, "*.pdf"))):
        base = os.path.basename(path)
        if names is not None and base not in names:
            continue
        with open(path, 'rb') as fd:
            candidates[base] = fd.read()
    return candidates


def diff_against(carved, planted):
    """Return (differing_bytes, differing_pct, tail_only:bool, mismatch_offset).

    Model: carving mismatches are truncation-point differences (same content
    up to some offset, then diverges/ends), not scattered edits -- so a
    common-prefix + common-suffix measurement is both the correct model and
    O(n), unlike a full difflib alignment."""
    prefix = common_prefix_len(carved, planted)
    residual = min(len(carved), len(planted)) - prefix
    suffix = common_suffix_len(carved, planted, residual) if residual > 0 else 0
    total = max(len(carved), len(planted))
    differing = total - prefix - suffix
    pct = (differing / total * 100.0) if total else 0.0
    tail_only = (len(planted) > 0 and prefix >= TAIL_FRACTION * len(planted))
    return differing, pct, tail_only, prefix


def best_match(carved_bytes, candidates):
    """Return (name, prefix_len) for the candidate with the longest common
    byte prefix against carved_bytes -- fast, and the right signal for
    'same source, diverges partway through' matching."""
    best_name, best_len = None, 0
    for name, data in candidates.items():
        n = common_prefix_len(carved_bytes, data)
        if n > best_len:
            best_name, best_len = name, n
    return best_name, best_len


def load_dedup_csv(path):
    """Return {basename: row_dict} from an existing dedup_results.csv, or {}."""
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline='') as fd:
        for row in csv.DictReader(fd):
            out[row.get('file', '')] = row
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--carved-dir", required=True, help="folder containing carved *.pdf files")
    ap.add_argument("--src", required=True, help="folder containing the original planted PDFs")
    ap.add_argument("--manifest", default=None,
                    help="generator's _manifest.csv (restricts candidates to what was "
                         "actually planted in this image; omit to use all of --src)")
    ap.add_argument("--dedup-csv", default=None,
                    help="existing dedup_results.csv to merge in T3/T4 columns (verdict, "
                         "dedup_label) by filename")
    ap.add_argument("--min-prefix-bytes", type=int, default=256,
                    help="minimum common-prefix length (bytes) to accept a T2 candidate "
                         "match (default 256 -- well past the %%PDF- header alone)")
    ap.add_argument("--out", required=True, help="output CSV path")
    args = ap.parse_args()

    candidates = load_candidates(args.src, args.manifest)
    if not candidates:
        raise SystemExit("no candidate ground-truth PDFs found in %s" % args.src)

    candidate_hashes = {name: sha256_bytes(data) for name, data in candidates.items()}
    dedup_rows = load_dedup_csv(args.dedup_csv)

    carved_paths = sorted(glob.glob(os.path.join(args.carved_dir, "*.pdf")))
    if not carved_paths:
        raise SystemExit("no carved *.pdf files found in %s" % args.carved_dir)

    rows = []
    for path in carved_paths:
        fname = os.path.basename(path)
        with open(path, 'rb') as fd:
            carved_bytes = fd.read()
        carved_hash = sha256_bytes(carved_bytes)

        exact_name = next((n for n, h in candidate_hashes.items() if h == carved_hash), None)

        row = dict(
            carved_file=fname, carved_bytes=len(carved_bytes),
            tier="", matched_ground_truth="", match_prefix_bytes="",
            differing_bytes="", differing_pct="", diff_location="",
        )

        if exact_name:
            row.update(tier="T1_EXACT", matched_ground_truth=exact_name,
                       match_prefix_bytes=len(carved_bytes), differing_bytes=0,
                       differing_pct=0.0, diff_location="none")
        else:
            name, prefix_len = best_match(carved_bytes, candidates)
            if name is None or prefix_len < args.min_prefix_bytes:
                row.update(tier="NO_MATCH", matched_ground_truth="",
                           match_prefix_bytes=prefix_len)
            else:
                differing, pct, tail_only, _ = diff_against(carved_bytes, candidates[name])
                row.update(
                    tier="T2_BYTE_DIFF", matched_ground_truth=name,
                    match_prefix_bytes=prefix_len, differing_bytes=differing,
                    differing_pct=round(pct, 3),
                    diff_location=("trailer/padding" if tail_only else "content"),
                )

        # Merge T3/T4 (verifier + dedup) if a dedup_results.csv was supplied
        dd = dedup_rows.get(fname)
        row['verdict'] = dd.get('verdict', '') if dd else ''
        row['dedup_label'] = dd.get('dedup_label', '') if dd else ''

        rows.append(row)

    cols = ["carved_file", "carved_bytes", "tier", "matched_ground_truth",
            "match_prefix_bytes", "differing_bytes", "differing_pct", "diff_location",
            "verdict", "dedup_label"]
    with open(args.out, 'w', newline='') as fd:
        w = csv.DictWriter(fd, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    n_exact = sum(1 for r in rows if r['tier'] == "T1_EXACT")
    n_diff = sum(1 for r in rows if r['tier'] == "T2_BYTE_DIFF")
    n_none = sum(1 for r in rows if r['tier'] == "NO_MATCH")
    print("%d carved files: %d exact (T1), %d byte-diff match (T2), %d no match"
          % (len(rows), n_exact, n_diff, n_none))
    print("-> %s" % args.out)


if __name__ == "__main__":
    main()
