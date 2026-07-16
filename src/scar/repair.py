import re
import zlib

from Carver import Reassembler

SECTOR = 512
# Regex to find the start of a stream body
_STREAM_RE = re.compile(rb'stream\r?\n')

_IMAGE_CACHE = {}


# Load full disk image into memory, cached for reuse across calls
def _load_image(image_path):
    img = _IMAGE_CACHE.get(image_path)
    if img is None:
        with open(image_path, 'rb') as fd:
            img = fd.read()
        _IMAGE_CACHE.clear()
        _IMAGE_CACHE[image_path] = img
    return img


# Check whether data is a complete, valid zlib stream
def _zlib_eof(data):
    try:
        d = zlib.decompressobj()
        out = d.decompress(data)
        out += d.flush()
        return d.eof and len(out) > 0
    except zlib.error:
        return False


# Try to repair a bifragmented FlateDecode stream between two anchors
def _repair_gap(image, carved, p1, p2, d1, d2):
    gap_len = p2 - p1
    if gap_len <= 0 or gap_len > (8 << 20):
        return None

    m = _STREAM_RE.search(carved, p1, min(p1 + 4096, p2))
    if not m:
        return None
    zoff = m.end() - p1

    if _zlib_eof(bytes(carved[p1 + zoff:p2 + (64 << 10)])):
        return None

    # Try sector-aligned split points between the two anchors
    first = (-d1) % SECTOR
    a = first if first > zoff else first + SECTOR
    while a <= gap_len:
        fragA = image[d1:d1 + a]
        fragB = image[d2 - (gap_len - a):d2]
        repaired = fragA + fragB
        if len(repaired) == gap_len and _zlib_eof(repaired[zoff:]):
            return repaired
        a += SECTOR
    return None


# Repair bifragmented streams in a carved PDF in place; returns count of fixes
def repair_pdf(image_path, map_path, pdf_path):
    with open(image_path, 'rb') as fd:
        r = Reassembler(fd)
        r.load_map(map_path)
    image = _load_image(image_path)

    anchors = sorted(set(r.points))
    if len(anchors) < 2:
        return 0

    with open(pdf_path, 'rb') as fd:
        carved = bytearray(fd.read())

    repaired = 0
    for i in range(len(anchors) - 1):
        p1, p2 = anchors[i], anchors[i + 1]
        d1, d2 = r.mapping.get(p1), r.mapping.get(p2)
        if d1 is None or d2 is None:
            continue
        if (d2 - d1) == (p2 - p1):
            continue  # Contiguous anchors — nothing to repair
        if p2 > len(carved):
            continue
        fix = _repair_gap(image, carved, p1, p2, d1, d2)
        if fix is not None:
            carved[p1:p2] = fix
            repaired += 1

    if repaired:
        with open(pdf_path, 'wb') as fd:
            fd.write(bytes(carved))
    return repaired
