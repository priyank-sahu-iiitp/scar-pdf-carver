#!/usr/bin/env python3
import hashlib
import os
import sys

CONFIRMED = 'CONFIRMED'
PARTIAL   = 'PARTIAL'
REJECT    = 'REJECT'


# ── P1-P4 via pikepdf ────────────────────────────────────────────────────────

def _pikepdf_checks(path):
    res = {
        'open_ok': False, 'error': '',
        'p1': False, 'p2': False, 'p3': False, 'p4': False,
        'pages': 0, 'failed_refs': 0, 'failed_streams': 0, 'total_streams': 0,
    }
    try:
        import pikepdf
    except ImportError:
        res['error'] = 'pikepdf not installed'
        return res

    try:
        with pikepdf.open(path, suppress_warnings=True) as pdf:
            res['open_ok'] = True

            # P4: page tree resolves and is non-empty
            try:
                res['pages'] = len(pdf.pages)
                res['p4'] = res['pages'] > 0
            except Exception:
                res['p4'] = False

            # P1: every object in the xref table is accessible (no dangling refs)
            failed_refs = 0
            try:
                for obj in pdf.objects:
                    try:
                        repr(obj)
                    except pikepdf.PdfError:
                        failed_refs += 1
            except Exception:
                pass
            res['failed_refs'] = failed_refs
            res['p1'] = failed_refs == 0

            # P2: every content stream fully decodes (all filters applied)
            total_s = failed_s = 0
            try:
                for obj in pdf.objects:
                    if isinstance(obj, pikepdf.Stream):
                        total_s += 1
                        try:
                            obj.read_bytes()
                        except Exception:
                            failed_s += 1
            except Exception:
                pass
            res['total_streams'] = total_s
            res['failed_streams'] = failed_s
            res['p2'] = failed_s == 0

            # P3: ObjStm (object streams) decode — check /Type /ObjStm specifically
            failed_objstm = 0
            try:
                for obj in pdf.objects:
                    if (isinstance(obj, pikepdf.Stream) and
                            obj.get('/Type') == pikepdf.Name('/ObjStm')):
                        try:
                            obj.read_bytes()
                        except Exception:
                            failed_objstm += 1
            except Exception:
                pass
            res['p3'] = failed_objstm == 0

    except Exception as e:
        res['error'] = str(e).splitlines()[0][:120]
    return res


# ── P5-P6 via pymupdf ────────────────────────────────────────────────────────

def _pymupdf_checks(path, gt_hashes=None):
    res = {
        'p5': False, 'p6': None,
        'render_hashes': [], 'render_pages': 0, 'error': '',
    }
    try:
        import fitz
    except ImportError:
        res['error'] = 'pymupdf not installed'
        return res

    try:
        doc = fitz.open(path)
        hashes = []
        render_ok = True
        for page in doc:
            try:
                pix = page.get_pixmap(dpi=72)
                hashes.append(hashlib.md5(pix.samples).hexdigest())
            except Exception as e:
                res['error'] = str(e)[:120]
                render_ok = False
                break
        doc.close()
        res['p5'] = render_ok and len(hashes) > 0
        res['render_hashes'] = hashes
        res['render_pages'] = len(hashes)
        # P6: render-hash must match ground truth (controlled corpus only)
        if gt_hashes is not None:
            res['p6'] = (hashes == gt_hashes)
    except Exception as e:
        res['error'] = str(e).splitlines()[0][:120]
    return res


# ── Main verify entry point ───────────────────────────────────────────────────

def verify(path, gt_hashes=None):
    """
    Run P1-P6 on a single PDF.

    Returns a dict:
      verdict         : CONFIRMED | PARTIAL | REJECT
      p1..p6          : bool (p6 is None when no GT provided)
      pages           : int
      render_pages    : int
      failed_refs     : int   (P1 failures)
      failed_streams  : int   (P2 failures)
      total_streams   : int
      render_hashes   : list[str]
      reason          : str
    """
    result = {
        'path': path, 'verdict': REJECT,
        'p1': False, 'p2': False, 'p3': False,
        'p4': False, 'p5': False, 'p6': None,
        'pages': 0, 'render_pages': 0,
        'failed_refs': 0, 'failed_streams': 0, 'total_streams': 0,
        'render_hashes': [], 'reason': '',
    }

    # Quick structural pre-check before importing anything heavy
    try:
        with open(path, 'rb') as fd:
            head = fd.read(1024)
    except Exception as e:
        result['reason'] = 'cannot read file: %s' % e
        return result

    if b'%PDF-' not in head:
        result['reason'] = 'no %PDF- header'
        return result

    # P1-P4
    pk = _pikepdf_checks(path)
    for k in ('p1', 'p2', 'p3', 'p4', 'pages',
              'failed_refs', 'failed_streams', 'total_streams'):
        result[k] = pk.get(k, False)

    if not pk['open_ok']:
        result['reason'] = 'cannot open: %s' % pk.get('error', 'unknown')
        return result

    if not result['p4']:
        result['reason'] = 'P4 fail: page tree empty or unresolvable'
        return result

    # P5-P6
    py = _pymupdf_checks(path, gt_hashes)
    for k in ('p5', 'p6', 'render_hashes', 'render_pages'):
        result[k] = py.get(k)

    if not result['p5']:
        result['reason'] = 'P5 fail: %s' % (py.get('error') or 'render exception')
        return result

    # Assign verdict
    refs_ok    = result['p1']
    streams_ok = result['p2'] and result['p3']
    render_ok  = result['p5']

    if refs_ok and streams_ok and render_ok:
        if result['p6'] is False:
            # Renders correctly but pixel hash diverges from ground truth
            result['verdict'] = PARTIAL
            result['reason'] = 'P6 fail: render-hash mismatch vs ground truth'
        else:
            result['verdict'] = CONFIRMED
            result['reason'] = 'CONFIRMED (%d pages, %d streams ok)' % (
                result['pages'], result['total_streams'])
    else:
        # Renders but has incomplete refs or corrupt streams → PARTIAL
        result['verdict'] = PARTIAL
        parts = []
        if not refs_ok:
            parts.append('P1: %d dangling ref(s)' % result['failed_refs'])
        if not streams_ok:
            parts.append('P2/P3: %d/%d stream(s) fail decode' % (
                result['failed_streams'], result['total_streams']))
        result['reason'] = '; '.join(parts)

    return result


# ── Batch helper ──────────────────────────────────────────────────────────────

def verify_batch(paths, gt_map=None, quiet=False):
    """
    Verify a list of PDF paths.
    gt_map: {path: [render_hashes]} — provide to enable P6.
    Returns list of result dicts.
    """
    results = []
    for path in paths:
        gt = (gt_map or {}).get(path)
        r = verify(path, gt_hashes=gt)
        if not quiet:
            print("%-12s  %s" % (r['verdict'], os.path.basename(path)))
            if r['verdict'] != CONFIRMED:
                print("             %s" % r['reason'])
        results.append(r)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description="Strict P1-P6 PDF verifier. "
                    "Outputs CONFIRMED / PARTIAL / REJECT per file.")
    ap.add_argument("files", nargs='+', help="PDF file(s) to verify")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-file output, print summary only")
    args = ap.parse_args()

    missing = [f for f in args.files if not os.path.exists(f)]
    if missing:
        sys.exit("ERROR: not found: %s" % ', '.join(missing))

    results = verify_batch(args.files, quiet=args.quiet)

    c = sum(1 for r in results if r['verdict'] == CONFIRMED)
    p = sum(1 for r in results if r['verdict'] == PARTIAL)
    j = sum(1 for r in results if r['verdict'] == REJECT)
    print("\n%d file(s): CONFIRMED=%d  PARTIAL=%d  REJECT=%d"
          % (len(results), c, p, j))
