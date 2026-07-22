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

import bisect


class Reassembler:
    # Maps PDF logical offsets to physical disk image offsets
    interpolate_forward = True
    overread = 100 * 1024

    def __init__(self, fd):
        self.fd = fd
        self.points = []
        self.mapping = {}
        self.readptr = 0
        self.max_image_offset = None  # Document-end bound from map file

    def del_point(self, file_pos):
        try:
            idx = self.points.index(file_pos)
            del self.mapping[file_pos]
            self.points.pop(idx)
        except (ValueError, KeyError):
            pass

    def add_point(self, file_pos, image_pos):
        bisect.insort_left(self.points, file_pos)
        if file_pos in self.mapping:
            expected, left = self.interpolate(file_pos - 1)
            if abs(image_pos - expected) > abs(self.mapping[file_pos] - expected):
                return
        self.mapping[file_pos] = image_pos

    def seek(self, offset, whence=0):
        if whence == 0:
            self.readptr = offset
        elif whence == 1:
            self.readptr += offset

    def interpolate(self, file_offset, direction_forward=None):
        if direction_forward is None:
            direction_forward = self.interpolate_forward
        if file_offset < self.points[0]:
            direction_forward = False
        elif file_offset > self.points[-1]:
            direction_forward = True
        if direction_forward:
            l = bisect.bisect_right(self.points, file_offset) - 1
            try:
                left = self.points[l + 1] - file_offset
            except IndexError:
                left = self.overread
            return (self.mapping[self.points[l]] + file_offset - self.points[l]), left
        else:
            r = bisect.bisect_right(self.points, file_offset)
            return (self.mapping[self.points[r]] - (self.points[r] - file_offset),
                    self.points[r] - file_offset)

    def tell(self):
        return self.readptr

    def read(self, length):
        result = bytearray()
        while length > 0:
            try:
                m, left = self.interpolate(self.readptr)
                # Stop at document-end bound to prevent over-reading into image noise
                if self.max_image_offset is not None:
                    if m >= self.max_image_offset:
                        break
                    left = min(left, self.max_image_offset - m)
                self.fd.seek(m, 0)
            except Exception:
                left = 1000
            want_to_read = min(left, length)
            if want_to_read <= 0:
                self.readptr += 1
                length -= 1
                continue
            data = self.fd.read(want_to_read)
            if not data:
                break
            self.readptr += min(left, length)
            result.extend(data)
            length -= len(data)
        return bytes(result)

    # Load offset mapping from a .map file
    def load_map(self, mapfile):
        with open(mapfile, 'r') as fd:
            for line in fd:
                if line.startswith("#"):
                    s = line.strip()
                    if s.startswith("# BOUND "):
                        try:
                            self.max_image_offset = int(s[8:])
                        except ValueError:
                            pass
                    continue
                parts = line.split(" ", 2)
                if len(parts) < 2:
                    continue
                self.add_point(int(parts[0]), int(parts[1]))
