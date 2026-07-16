#!/usr/bin/env python3
import sys
from optparse import OptionParser
import Carver
import PDF


def check_errors(p, end_offset):
    p.restore_state()
    while p.processed < end_offset and p.error < 10:
        p.next_token()
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

    for i in range(0, c.points[-1] // 512 + 10):
        y_forward, left = c.interpolate(i * 512, True)
        y_reverse, left = c.interpolate(i * 512, False)

        if y_forward != y_reverse:
            sys.stderr.write("Sector %s is ambiguous - testing\n" % i)
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

    while True:
        token = p.next_token()
        fd.write(p.processed_buffer)
        p.processed_buffer = b''

        if token == 'RESET_STATE' and p.processed > c.points[-1]:
            break
        if token is None and p.empty():
            break

    sys.stderr.write("Total reconstructed file error count: %s\n" % p.error)
    fd.close()
