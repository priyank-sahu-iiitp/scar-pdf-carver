#!/usr/bin/env python3
from optparse import OptionParser
import re
import sys
import pickle
import PDF
from Carver import Reassembler

SECTOR_SIZE = 512

parser = OptionParser(usage="""%prog """)
parser.add_option('-i', '--index', default=None,
                  help='Index file to operate on')
parser.add_option('-c', '--create', default=False, action="store_true",
                  help='Create a new index file')
parser.add_option('-m', '--maps', default=False, action="store_true",
                  help='Carve the index file by creating initial map files')
parser.add_option('-p', '--print', default=False, action="store_true",
                  help='Print the index hits')

(options, args) = parser.parse_args()

if not options.index:
    print("Need an index file to operate on.")
    sys.exit(1)

regexs = {
    'OBJECTS':   rb'(\d+) (\d+) obj',
    'XREFS':     rb'([\r\n ])xref([\r\n])',
    'STARTXREF': rb'startxref([\r\n])',
}

cregexs = {}
hits = {}


def build_index():
    for k, v in regexs.items():
        cregexs[k] = re.compile(v)

    BLOCK_SIZE = 409600
    offset = 0
    with open(args[0], 'rb') as fd:
        while True:
            data = fd.read(BLOCK_SIZE)
            if len(data) == 0:
                break
            for k, v in cregexs.items():
                for m in v.finditer(data):
                    print("Found %s in %s" % (k, offset + m.start()))
                    hits.setdefault(k, []).append(offset + m.start())
            offset += len(data)

    with open(options.index, 'wb') as fd:
        pickle.dump(hits, fd)

    print(hits)


def save_map(filename, pdf, image_fd, hits):
    description = '\n#'.join(pdf.__str__().splitlines())
    with open("%s.map" % filename, 'w') as fd:
        fd.write("#" + description + "\n")
        for id_, offset, generation, typ in pdf.xref:
            print("%s - %s %s" % (id_, offset, typ))
            if typ == 'n':
                possibles = [x for x in hits.get('OBJECTS', [])
                             if x % 512 == offset % 512]
                for possibility in possibles:
                    image_fd.seek(possibility, 0)
                    line = image_fd.readline()
                    expected = ("%d %d" % (id_, generation)).encode('latin-1')
                    if not line.startswith(expected):
                        continue
                    fd.write("%s %s Object_%s\n" % (offset, possibility, id_))


def print_xrefs():
    with open(options.index, 'rb') as fd:
        hits_local = pickle.load(fd)

    image_fd = open(args[0], 'rb')
    total_xrefs = {}

    for xref in hits_local.get('XREFS', []):
        print("Reading XREFs table from %s " % xref)
        image_fd.seek(xref, 0)
        p = PDF.PDFParser(image_fd)
        while p.next_token() != "RESET_STATE" and p.error < 10:
            pass
        print(p.objects)
        p.START_XREF(None, None)
        print(p.pdf)
        total_xrefs[xref] = p.pdf
        save_map(xref, p.pdf, image_fd, hits_local)

    print(total_xrefs.keys())
    for x in list(total_xrefs.keys()):
        print("Node %s has the following xrefs: " % x)
        for xref_offset in total_xrefs[x].xref_offsets:
            for y in list(total_xrefs.keys()):
                if y > x:
                    continue
                print("Checking %s against xref at %s (%s,%s)"
                      % (xref_offset, y, xref_offset % 512, y % 512))
                if xref_offset % SECTOR_SIZE == y % SECTOR_SIZE + 1:
                    if (total_xrefs[y].xref_range[0] == total_xrefs[x].xref_range[1]
                            or total_xrefs[y].xref_range[1] == total_xrefs[x].xref_range[0]):
                        new_pdf = PDF.PDFFile()
                        new_pdf.xref = total_xrefs[x].xref + total_xrefs[y].xref
                        new_pdf.xref_range = [
                            min(total_xrefs[y].xref_range[0], total_xrefs[x].xref_range[0]),
                            max(total_xrefs[y].xref_range[1], total_xrefs[x].xref_range[1]),
                        ]
                        save_map("%s-%s" % (x, y), new_pdf, image_fd, hits_local)

    image_fd.close()


if options.create:
    build_index()
elif options.maps:
    print_xrefs()
elif getattr(options, 'print'):
    with open(options.index, 'rb') as fd:
        hits = pickle.load(fd)
    print(hits)
else:
    print("Nothing to do... try -h")
