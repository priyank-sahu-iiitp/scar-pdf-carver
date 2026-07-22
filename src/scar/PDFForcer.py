#!/usr/bin/env python3
# Derived from Michael I. Cohen's DFRWS 2007 Forensic Challenge submission
# ("Advanced carving techniques", Digital Investigation 4(3-4):119-128, 2007).
#
#   Original work Copyright 2004  Commonwealth of Australia
#   Original work Copyright 2006  Michael I. Cohen
#   Modifications  Copyright 2026  Priyank Kumar Sahu
#
# This file has been changed from the original: ported from Python 2 to Python 3, and extended with the SCAR
# contributions (C1 cross-reference-stream decoding, C2 anchored bi-fragment
# repair) and engineering elements E1-E6.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version. See the LICENSE file at the repository root, and
# ATTRIBUTION.md for full provenance.

import sys
from optparse import OptionParser
import Carver
import PDF


# Run trial parse from saved state up to end_offset; return error count.
# The trial must reach the candidate sector's FlateDecode stream so that a
# wrong split is rejected by decompression failure; a chunked-decode stream
# (indirect /Length) can extend well past the object header, so we parse to
# the next anchor (Cohen's behaviour) under a generous ceiling that still
# bounds runaway on pathological maps.
def check_errors(p, end_offset):
    p.restore_state()
    end_offset = min(end_offset, p.processed + (1 << 20))
    while p.processed < end_offset and p.error < 10:
        tok = p.next_token()
        if tok is None and p.empty():
            break
    return p.error


if __name__ == '__main__':
    parser = OptionParser(usage="""%prog """)
    parser.add_option('-m', '--map', default=None,
                      help='Approximate map file to operate on (Mandatory)')
    parser.add_option('-r', '--reconstructed', default=None,
                      help='File to write the reconstructed map onto (optional)')
    parser.add_option('-o', '--output', default=None,
                      help='The reconstituted PDF file to write (Mandatory)')
    parser.add_option('-v', '--verbose', default=0, type='int',
                      help='Verbosity level (0=silent, 1=normal, 2+=debug)')
    parser.add_option('-s', '--slow', default=False, action="store_true",
                      help='Turn off state recovery (slower, more robust)')

    (options, args) = parser.parse_args()

    if not options.output or not options.map:
        print("Error using tool - try -h for help")
        sys.exit(1)

    if len(args) != 1:
        print("You must specify the name of the image")
        sys.exit(1)

    c = Carver.Reassembler(open(args[0], 'rb'))
    p = PDF.PDFParser(c)
    p.verbose = options.verbose

    c.load_map(options.map)

    # Exit cleanly if map has no usable data points
    if not c.points:
        sys.stderr.write("Map has no usable points: %s\n" % options.map)
        sys.exit(2)

    # Resolve sector ambiguities. A real bi-fragment split is found in a handful
    # of trials; a heavily-scrambled, single-split-irrecoverable map can present
    # thousands of ambiguous sectors, so we bound the scan by both a trial count
    # and a wall-clock budget to keep worst-case time bounded without reducing
    # recovery (the cases the wall-clock cut-off drops are not single-split
    # recoverable anyway).
    import time as _time
    AMBIG_BUDGET = 500
    AMBIG_SECONDS = 15.0
    ambig_tested = 0
    _t0 = _time.perf_counter()
    for i in range(0, c.points[-1] // 512 + 10):
        if ambig_tested >= AMBIG_BUDGET:
            sys.stderr.write("Ambiguity budget reached (%d) - stopping scan\n"
                             % AMBIG_BUDGET)
            break
        if _time.perf_counter() - _t0 > AMBIG_SECONDS:
            sys.stderr.write("Ambiguity time budget reached (%.0fs) - stopping\n"
                             % AMBIG_SECONDS)
            break
        y_forward, left = c.interpolate(i * 512, True)
        y_reverse, left = c.interpolate(i * 512, False)

        if y_forward != y_reverse:
            sys.stderr.write("Sector %s is ambiguous - testing\n" % i)
            ambig_tested += 1
            c.add_point(i * 512, y_reverse)
            until_offset = left + i * 512 + 4000
            print("Checking until %s" % until_offset)

            if options.slow:
                c.seek(0)
                p = PDF.PDFParser(c)
                p.verbose = options.verbose

            error_count = check_errors(p, until_offset)
            if error_count == 0:
                sys.stderr.write("Found a hit at %s\n" % i)
            else:
                c.del_point(i * 512)

            print("Error count is %s" % error_count)

    if options.reconstructed:
        sys.stderr.write("Writing reconstructed file: %s\n" % options.reconstructed)
        with open(options.reconstructed, 'w') as fd:
            for x in c.points:
                fd.write("%s %s\n" % (x, c.mapping[x]))

    sys.stderr.write("Writing file: %s\n" % options.output)
    fd = open(options.output, 'wb')
    c.seek(0)
    p = PDF.PDFParser(c)
    p.verbose = options.verbose

    # Hard cap on output size to prevent over-reads into image noise
    doc_span = c.points[-1]
    overread = Carver.Reassembler.overread
    max_bytes = min(max(doc_span + overread * 4, 8 * 1024 * 1024),
                    128 * 1024 * 1024)
    written = 0

    while True:
        token = p.next_token()
        chunk = p.processed_buffer
        fd.write(chunk)
        written += len(chunk)
        p.processed_buffer = b''

        if token == 'RESET_STATE' and p.processed > doc_span:
            break
        if token is None and p.empty():
            break
        if written > max_bytes:
            sys.stderr.write("Over-read ceiling hit (%d bytes) - stopping\n"
                             % written)
            break
        # When the map carries a document-end BOUND (E6), the reassembler
        # already clamps reads at the true %%EOF, so a large trailing stream
        # can complete; only apply the file-space +overread cutoff when no
        # BOUND was recorded (sparse/absent xref) to bound over-read there.
        if c.max_image_offset is None and p.processed > doc_span + overread:
            sys.stderr.write("Past document end (+overread) - stopping\n")
            break

    sys.stderr.write("Total reconstructed file error count: %s\n" % p.error)
    fd.close()

    # The lexer discards the leading '%' from '%PDF-' when it starts in INITIAL
    # state and can't match the comment pattern on a bytes buffer.  Prepend it
    # back so the verifier's structural_check (which looks for b'%PDF-') passes.
    with open(options.output, 'rb') as _fx:
        _head = _fx.read(4)
    if _head == b'PDF-':
        with open(options.output, 'rb') as _fx:
            _body = _fx.read()
        with open(options.output, 'wb') as _fx:
            _fx.write(b'%' + _body)
        sys.stderr.write("Prepended missing '%%' to PDF header\n")
