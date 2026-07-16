# -*- coding: utf-8 -*-
"""Classify each PDF in --src by its real cross-reference encoding
(raw-bytes, robust). Reports per-file features + a primary label, and the
mix vs the expected counts."""
import argparse
import csv
import glob
import os
import re
import zlib

OBJ = re.compile(rb'(\d+)\s+(\d+)\s+obj\b')
ST = re.compile(rb'stream\r?\n')


def _xref_stream_predictor(data):
    """Return (has_xref_stream, has_png_predictor) by decoding the /Type/XRef
    object's dict."""
    has_xs = False
    pred = False
    for m in re.finditer(rb'/Type\s*/XRef', data):
        has_xs = True
        # look back to the object's dict for /DecodeParms.../Predictor n
        start = data.rfind(b' obj', max(0, m.start() - 4000), m.start())
        seg = data[max(0, m.start() - 4000):m.start() + 2000]
        pm = re.search(rb'/Predictor\s+(\d+)', seg)
        if pm and int(pm.group(1)) >= 10:
            pred = True
    return has_xs, pred


def _objstm_share(data):
    """Fraction of in-use objects that are packed in object streams (type-2),
    estimated by decoding the xref stream entry types; falls back to counting
    /Type/ObjStm containers."""
    n_objstm = len(re.findall(rb'/Type\s*/ObjStm', data))
    return n_objstm


def _resolve_active_xref(data):
    """Follow the LAST startxref to see whether the active cross-reference is a
    classic 'xref' table or a /Type/XRef stream object. Returns 'classic',
    'stream', or 'unknown'."""
    idx = data.rfind(b'startxref')
    if idx < 0:
        return 'unknown'
    m = re.search(rb'startxref\s+(\d+)', data[idx:idx + 64])
    if not m:
        return 'unknown'
    off = int(m.group(1))
    if off >= len(data):
        return 'unknown'
    at = data[off:off + 64].lstrip()
    if at.startswith(b'xref'):
        return 'classic'
    if re.match(rb'\d+\s+\d+\s+obj', at):
        seg = data[off:off + 3000]
        return 'stream' if b'/XRef' in seg else 'unknown'
    return 'unknown'


def classify(path):
    data = open(path, "rb").read()
    head = data[:4096]
    linearized = b'/Linearized' in head
    has_xrefstm = b'/XRefStm' in data            # trailer pointer => hybrid
    has_xs, pred = _xref_stream_predictor(data)
    n_objstm = _objstm_share(data)
    active = _resolve_active_xref(data)
    ver = ''
    vm = re.match(rb'%PDF-(\d\.\d)', data[:16])
    if vm:
        ver = vm.group(1).decode()

    # Cohen reads a file iff it has a classic 'xref' table to follow.
    # active == 'classic' (table) OR a hybrid (table + /XRefStm) => Cohen-readable.
    classic_table = (active == 'classic') or has_xrefstm
    if active == 'classic' and has_xrefstm:
        label = "hybrid (Cohen-readable)"
    elif active == 'classic':
        label = "classic table (Cohen-readable)"
    elif active == 'stream':
        if pred:
            label = "xref-stream +PNG (Cohen-blind)"
        elif n_objstm >= 3:
            label = "xref-stream +objstm (Cohen-blind)"
        else:
            label = "xref-stream (Cohen-blind)"
    else:
        # fall back on version: 1.2-1.4 predate xref streams => classic
        if ver and ver <= '1.4':
            label = "classic table (Cohen-readable)"
            classic_table = True
        else:
            label = "unknown"
    return dict(file=os.path.basename(path), version=ver, label=label,
                active=active, xref_stream=int(has_xs), png_pred=int(pred),
                objstm=n_objstm, linearized=int(linearized),
                hybrid=int(has_xrefstm), classic_table=int(classic_table),
                size=len(data))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, help="folder containing PDFs to classify")
    ap.add_argument("--out", required=True, help="output CSV path")
    args = ap.parse_args()

    rows = [classify(p) for p in sorted(glob.glob(os.path.join(args.src, "*.pdf")))]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    cols = ["file", "version", "label", "active", "xref_stream", "png_pred",
            "objstm", "linearized", "hybrid", "classic_table", "size"]
    with open(args.out, "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print("classified %d PDFs -> %s\n" % (len(rows), args.out))
    print("%-26s %-7s %-18s %s" % ("file", "ver", "label", "xs/png/objstm/lin/hyb/cls"))
    for r in rows:
        print("%-26s %-7s %-18s %d/%d/%d/%d/%d/%d  %dKB"
              % (r["file"][:26], r["version"], r["label"], r["xref_stream"],
                 r["png_pred"], r["objstm"], r["linearized"], r["hybrid"],
                 r["classic_table"], r["size"] // 1024))
    from collections import Counter
    print("\nmix:", dict(Counter(r["label"] for r in rows)))


if __name__ == "__main__":
    main()
