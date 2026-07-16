#!/usr/bin/env python3
import zlib
import re

# Regex to locate FlateDecode stream bodies for zlib validation
_STREAM_RE = re.compile(rb'/FlateDecode\b.*?\bstream\r?\n(.*?)\r?\nendstream',
                        re.DOTALL)


# Quick check for %PDF- header and %%EOF trailer
def structural_check(data):
    return (b'%PDF-' in data[:1024], b'%%EOF' in data[-2048:])


# Decompress all FlateDecode streams and count failures
def flate_scan(data):
    total = failed = 0
    for m in _STREAM_RE.finditer(data):
        total += 1
        try:
            zlib.decompress(m.group(1))
        except zlib.error:
            failed += 1
    return total, failed


# Validate a recovered PDF using pikepdf (falls back to structural check if unavailable)
def verify_pdf_file(path):
    with open(path, 'rb') as fd:
        data = fd.read()

    has_header, has_eof = structural_check(data)
    flate_total, flate_fail = flate_scan(data)
    result = {
        'ok': False,
        'size': len(data),
        'has_header': has_header,
        'has_eof': has_eof,
        'flate_total': flate_total,
        'flate_fail': flate_fail,
        'pages': 0,
        'reason': '',
    }

    if not has_header:
        result['reason'] = 'no %PDF- header'
        return result

    try:
        import pikepdf
        with pikepdf.open(path) as pdf:
            result['pages'] = len(pdf.pages)
        if result['pages'] > 0:
            result['ok'] = True
            result['reason'] = 'valid (%d pages)' % result['pages']
        else:
            result['reason'] = 'opens but has no pages'
    except ImportError:
        # Structural fallback when pikepdf is not installed
        if has_header and has_eof and flate_fail == 0:
            result['ok'] = True
            result['reason'] = 'valid (structural; pikepdf unavailable)'
        else:
            bad = []
            if not has_eof:
                bad.append('no %%EOF trailer')
            if flate_fail:
                bad.append('%d/%d flate streams corrupt'
                           % (flate_fail, flate_total))
            result['reason'] = '; '.join(bad) or 'structural check failed'
    except Exception as e:
        result['reason'] = 'pikepdf: %s' % str(e).splitlines()[0][:90]

    return result


# Verify PDF supplied as bytes via a temp file
def verify_pdf(data):
    import tempfile
    import os
    fd, tmp = tempfile.mkstemp(suffix='.pdf')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        return verify_pdf_file(tmp)
    finally:
        os.unlink(tmp)


if __name__ == '__main__':
    import sys
    for path in sys.argv[1:]:
        r = verify_pdf_file(path)
        print("%s  %s  (%d bytes, %d/%d flate ok)"
              % (path, 'VALID' if r['ok'] else 'SUSPECT: ' + r['reason'],
                 r['size'], r['flate_total'] - r['flate_fail'],
                 r['flate_total']))
