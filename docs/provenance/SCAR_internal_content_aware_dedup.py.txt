#!/usr/bin/env python3
import hashlib
import os
import sys

UNIQUE      = 'UNIQUE'
EXACT_DUP   = 'EXACT_DUP'
LOGICAL_DUP = 'LOGICAL_DUP'
CONTAINMENT = 'CONTAINMENT'
CORRUPT     = 'CORRUPT'

_LABELS_ORDERED = (UNIQUE, EXACT_DUP, LOGICAL_DUP, CONTAINMENT, CORRUPT)


# ── Fingerprint helpers ───────────────────────────────────────────────────────

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fd:
        for chunk in iter(lambda: fd.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _object_set(path):
    # Return frozenset of MD5 hashes of all indirect objects (pikepdf)
    try:
        import pikepdf
        hashes = set()
        with pikepdf.open(path, suppress_warnings=True) as pdf:
            for obj in pdf.objects:
                try:
                    raw = repr(obj).encode('utf-8', errors='replace')
                    hashes.add(hashlib.md5(raw).hexdigest())
                except Exception:
                    pass
        return frozenset(hashes)
    except Exception:
        return frozenset()


# ── Core dedup logic ──────────────────────────────────────────────────────────

def dedup(pdf_results, quiet=False):
    """
    Content-aware dedup over a list of verifier result dicts.

    Checks in order (cheapest first, stop at first match):
      1. SHA256 byte hash    → EXACT_DUP
      2. Per-page render hash → LOGICAL_DUP   (reuses hashes from verifier — no re-render)
      3. Object-set Jaccard  → CONTAINMENT    (objset(A) ⊂ objset(B) → A is CONTAINMENT)

    REJECT files from the verifier are labelled CORRUPT and excluded from the registry
    so they never shadow a valid recovery.

    Processing order: CONFIRMED before PARTIAL, so the higher-quality file always
    enters the registry first and the redundant one is dropped.

    Args:
      pdf_results : list of dicts from verifier.verify()
                    Required keys: 'path', 'verdict', 'render_hashes'
      quiet       : suppress per-file output

    Returns:
      list of result dicts (same order as input after sorting by quality),
      each with two extra keys:
        dedup_label  : UNIQUE | EXACT_DUP | LOGICAL_DUP | CONTAINMENT | CORRUPT
        dedup_reason : human-readable explanation
    """
    _VERDICT_RANK = {'CONFIRMED': 0, 'PARTIAL': 1, 'REJECT': 2}
    sorted_results = sorted(
        pdf_results,
        key=lambda r: _VERDICT_RANK.get(r.get('verdict', 'REJECT'), 2)
    )

    seen_sha256  = {}   # sha256_hex  → path
    seen_render  = {}   # tuple(hashes) → path
    seen_objsets = []   # [(frozenset, path), ...]  for containment

    labeled = []

    for r in sorted_results:
        path    = r.get('path', '')
        verdict = r.get('verdict', 'REJECT')
        out     = dict(r)
        out['dedup_label']  = UNIQUE
        out['dedup_reason'] = ''

        # REJECT → CORRUPT, skip all registry checks
        if verdict == 'REJECT':
            out['dedup_label']  = CORRUPT
            out['dedup_reason'] = 'verifier REJECT'
            labeled.append(out)
            if not quiet:
                _print_label(CORRUPT, path, out['dedup_reason'])
            continue

        # Stage 1: SHA256 — exact byte match
        try:
            h256 = _sha256(path)
        except Exception as e:
            out['dedup_label']  = CORRUPT
            out['dedup_reason'] = 'cannot read: %s' % e
            labeled.append(out)
            continue

        if h256 in seen_sha256:
            out['dedup_label']  = EXACT_DUP
            out['dedup_reason'] = 'byte-identical to %s' % os.path.basename(seen_sha256[h256])
            labeled.append(out)
            if not quiet:
                _print_label(EXACT_DUP, path, out['dedup_reason'])
            continue

        # Stage 2: render-hash — same visual content, different bytes
        rh_key = tuple(r.get('render_hashes') or [])
        if rh_key and rh_key in seen_render:
            out['dedup_label']  = LOGICAL_DUP
            out['dedup_reason'] = 'renders identically to %s' % os.path.basename(
                seen_render[rh_key])
            labeled.append(out)
            if not quiet:
                _print_label(LOGICAL_DUP, path, out['dedup_reason'])
            continue

        # Stage 3: object-set — subset containment
        objset = _object_set(path)
        container = None
        if objset:
            for seen_set, seen_path in seen_objsets:
                if objset.issubset(seen_set):
                    container = seen_path
                    break

        if container:
            out['dedup_label']  = CONTAINMENT
            out['dedup_reason'] = 'object-set subset of %s' % os.path.basename(container)
            labeled.append(out)
            if not quiet:
                _print_label(CONTAINMENT, path, out['dedup_reason'])
            continue

        # UNIQUE — add to all registries
        seen_sha256[h256] = path
        if rh_key:
            seen_render[rh_key] = path
        if objset:
            seen_objsets.append((objset, path))

        out['dedup_label']  = UNIQUE
        out['dedup_reason'] = ''
        labeled.append(out)
        if not quiet:
            _print_label(UNIQUE, path, '')

    return labeled


def _print_label(label, path, reason):
    suffix = ('  (%s)' % reason) if reason else ''
    print("%-14s  %s%s" % (label, os.path.basename(path), suffix))


# ── Summary ───────────────────────────────────────────────────────────────────

def summarise(labeled, quiet=False):
    """Print and return counts per dedup label."""
    counts = {k: 0 for k in _LABELS_ORDERED}
    for r in labeled:
        counts[r.get('dedup_label', CORRUPT)] += 1
    if not quiet:
        print("\nDedup summary:")
        for label in _LABELS_ORDERED:
            print("  %-14s : %d" % (label, counts[label]))
    return counts


# ── CLI (standalone: runs verifier then dedup) ────────────────────────────────

if __name__ == '__main__':
    import argparse
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _HERE)
    import verifier

    ap = argparse.ArgumentParser(
        description="Content-aware PDF deduplication. "
                    "Runs verifier first, then dedup pipeline.")
    ap.add_argument("files", nargs='+', help="PDF files to process")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-file output")
    args = ap.parse_args()

    missing = [f for f in args.files if not os.path.exists(f)]
    if missing:
        sys.exit("ERROR: not found: %s" % ', '.join(missing))

    print("── verifier ─────────────────────────────")
    results = []
    for f in args.files:
        r = verifier.verify(f)
        if not args.quiet:
            print("%-12s  %s" % (r['verdict'], os.path.basename(f)))
        results.append(r)

    print("\n── dedup ────────────────────────────────")
    labeled = dedup(results, quiet=args.quiet)
    summarise(labeled)

    # Write labeled results to a CSV for the paper
    csv_path = os.path.join(os.path.dirname(args.files[0]), 'dedup_results.csv')
    try:
        with open(csv_path, 'w') as fd:
            fd.write("file,verdict,dedup_label,dedup_reason,pages,"
                     "failed_refs,failed_streams,render_pages\n")
            for r in labeled:
                fd.write("%s,%s,%s,%s,%d,%d,%d,%d\n" % (
                    os.path.basename(r.get('path', '')),
                    r.get('verdict', ''),
                    r.get('dedup_label', ''),
                    r.get('dedup_reason', '').replace(',', ';'),
                    r.get('pages', 0),
                    r.get('failed_refs', 0),
                    r.get('failed_streams', 0),
                    r.get('render_pages', 0),
                ))
        print("\nResults written to %s" % csv_path)
    except Exception as e:
        print("WARNING: could not write CSV: %s" % e)
