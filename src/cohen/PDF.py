# Derived from Michael I. Cohen's DFRWS 2007 Forensic Challenge submission
# ("Advanced carving techniques", Digital Investigation 4(3-4):119-128, 2007).
#
#   Original work Copyright 2004  Commonwealth of Australia
#   Original work Copyright 2006  Michael I. Cohen
#   Modifications  Copyright 2026  Priyank Kumar Sahu
#
# This file has been changed from the original: ported from Python 2 to Python 3; I/O plumbing to run over the
# evaluation corpus. Behaviour kept faithful to the original.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version. See the LICENSE file at the repository root, and
# ATTRIBUTION.md for full provenance.

import zlib
import os
import lexer


class PDFObject:
    def __init__(self, object_number, generation_number):
        self.object_number = object_number
        self.generation_number = generation_number
        self.contents = []
        self.stream = b''
        self.dictionary = {}
        self.decoded_stream = b''

    def append(self, value):
        self.contents.append(value)
        if type(value) == dict:
            self.dictionary = value

    def __str__(self):
        return "Object %s %s, contains: %r, %r" % (
            self.object_number, self.generation_number,
            self.contents, self.decoded_stream[:100])


class PDFFile:
    def __init__(self):
        self.trailer = []
        self.xref = []
        self.xref_offsets = []
        self.objects = []
        self.xref_range = [0, 0]

    def __str__(self):
        result = 'Output of PDF file\n'
        result += "Xref Table:\n"
        for i in self.xref:
            result += "%s\n" % (i,)
        result += "Trailer: %r\n" % self.trailer
        for o in self.objects:
            result += "%s\n" % o
        result += "Xref Offsets: %s\n" % (self.xref_offsets,)
        result += "Xref range: %s\n" % (self.xref_range,)
        return result


class PDFParser(lexer.SelfFeederMixIn, lexer.Lexer):
    state = "INITIAL"
    error = 0
    verbose = 1

    def __init__(self, fd, verbose=0):
        self.tokens = [
            ['OBJECT DECLERATION', r'^stream[\r\n]+', 'PUSH_STATE,STREAM_START', None],
            ['.*_STREAM', r'[\r\n]*endstream', 'END_STREAM,POP_STATE', None],
            ['.*_STREAM', r'.{1,200}?(?=[\r\n]*endstream)', 'STREAM', None],
            ['.*_STREAM', r'.{1,200}', 'STREAM', None],
            ['.', r'%%EOF[\r\n]+', 'RESET_STATE', 'INITIAL'],
            ['.', r'%[^\n\r]*', 'COMMENT', None],
            ['.', r'(\r|\r\n|\n)$', 'EOL', None],
            ['INITIAL', r'^(\d+) (\d+) obj',
             'save_state,PUSH_STATE,OBJECT_DECLERATION_START', 'OBJECT DECLERATION'],
            ['.', r'^endobj', 'OBJECT_DECLERATION_END,POP_STATE', None],
            ['.', r'^xref', 'PUSH_STATE', 'XREF_HEADER'],
            ['XREF_HEADER', r'(\d+) (\d+)', 'XREF_START', 'XREF_DATA'],
            ['.', r'^startxref', 'PUSH_STATE', 'START XREF'],
            ['START XREF', r'\d+', 'START_XREF,POP_STATE', None],
            ['XREF_DATA', r'(\d{10}) (\d{5}) ([a-zA-Z])', 'XREF_DATA', None],
            ['.', r'(\d+) (\d+) R', 'OBJECT_REFERENCE', None],
            ['.', r'(true|false)', 'BOOLEAN', None],
            ['.', r'null', 'NULL', None],
            ['.', r'\(', 'PUSH_STATE', 'LITERAL_STRING'],
            ['LITERAL_STRING', r'^[^\\\()]+', 'LITERAL_STRING', None],
            ['LITERAL_STRING', r'^\\[()]?', 'LITERAL_STRING', None],
            ['LITERAL_STRING', r'\)', 'LITERAL_STRING_END,POP_STATE', None],
            ['.', r'<(([0-9A-Fa-f]|\s)+)>', 'HEX_STRING', None],
            ['.', r'([-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+))', 'NUMBERS', None],
            ['.', r'/[#\.\w_\+-]+', 'NAME_OBJECT', None],
            ['.', r'\[', 'PUSH_STATE,ARRAY_START', 'ARRAY'],
            ['ARRAY', r'\]', 'ARRAY_END,POP_STATE', None],
            ['.', r'<<', 'PUSH_STATE,DICTIONARY_START', 'DICTIONARY'],
            ['DICTIONARY', r'>>', 'POP_STATE,DICTIONARY_END', None],
            ['.', r'^trailer', 'TRAILER,PUSH_STATE', 'TRAILER'],
            ['.', r'\s+', 'SPACE', None],
        ]
        self.objects = []
        self.fd = fd
        lexer.Lexer.__init__(self)
        self.pdf = PDFFile()
        self.string = b''
        self.xref_start_entry = 0
        self.xref_entries_to_go = 0
        self.verbose = verbose

    def ERROR(self, message=None):
        if message:
            print("Error: %s" % message)
        lexer.Lexer.ERROR(self)

    def RESET_STATE(self, t, m):
        self.objects = []

    def OBJECT_DECLERATION_START(self, t, m):
        self.add_to_current_object(PDFObject(m.group(1), m.group(2)))

    def OBJECT_DECLERATION_END(self, token, match):
        if not self.objects:
            return
        o = self.objects.pop()
        self.pdf.objects.append(o)

    def add_to_current_object(self, value):
        try:
            try:
                current_object = self.objects[-1]
            except IndexError:
                self.objects.append(value)
                return
            try:
                current_object.append(value)
                return
            except AttributeError:
                pass
            if current_object[1]:
                try:
                    if (self.state_stack[-1] == 'TRAILER'
                            and current_object[1] == b'/Prev'):
                        self.pdf.xref_offsets.append(value)
                except Exception:
                    pass
                current_object[current_object[1]] = value
                current_object[1] = None
            else:
                current_object[1] = value
        except Exception:
            pass

    def OBJECT_REFERENCE(self, token, m):
        self.add_to_current_object([int(m.group(1)), int(m.group(2))])

    def NUMBERS(self, token, m):
        self.add_to_current_object(float(m.group(0)))

    def LITERAL_STRING(self, token, m):
        self.string += m.group(0)

    def LITERAL_STRING_END(self, t, m):
        self.add_to_current_object(self.string)
        self.string = b''

    def NAME_OBJECT(self, t, m):
        self.add_to_current_object(m.group(0))

    def ARRAY_START(self, t, m):
        self.objects.append([])

    def ARRAY_END(self, t, m):
        self.add_to_current_object(self.objects.pop())

    def DICTIONARY_START(self, token, match):
        self.objects.append({1: 0})

    def DICTIONARY_END(self, token, match):
        d = self.objects.pop()
        if 1 in d:
            del d[1]
        self.add_to_current_object(d)

    def BOOLEAN(self, t, m):
        if m.group(0) == b'true':
            self.add_to_current_object(True)
        else:
            self.add_to_current_object(False)

    def HEX_STRING(self, t, m):
        hexstring = m.group(1)
        result = b''
        for i in range(0, len(hexstring) - 1, 2):
            try:
                result += bytes([int(hexstring[i:i + 2], 16)])
            except ValueError:
                pass
        self.add_to_current_object(result)

    def XREF_START(self, t, m):
        self.xref_start_entry = int(m.group(1))
        self.xref_entries_to_go = int(m.group(2))
        self.pdf.xref_range = [
            self.xref_start_entry,
            self.xref_start_entry + self.xref_entries_to_go
        ]

    def XREF_DATA(self, t, m):
        object_number = self.xref_start_entry
        offset = int(m.group(1))
        generation_number = int(m.group(2))
        type_char = m.group(3).decode('latin-1')
        self.pdf.xref.append([object_number, offset, generation_number, type_char])
        self.xref_start_entry += 1
        self.xref_entries_to_go -= 1
        if self.xref_entries_to_go == 0:
            return self.POP_STATE()

    def TRAILER(self, t, m):
        self.objects.append(self.pdf.trailer)

    def STREAM_START(self, t, m):
        obj = self.objects[-1] if self.objects else None
        if not hasattr(obj, 'dictionary'):
            return "GENERIC_STREAM"
        if obj.dictionary.get(b'/Filter') == b'/FlateDecode':
            self.dc = zlib.decompressobj()
            self.stream = b''
            self.decompressed = b''
            return "FLATE_STREAM"
        else:
            return "GENERIC_STREAM"

    def STREAM(self, t, m):
        if self.state == 'FLATE_STREAM':
            if self.verbose > 2:
                print("Got %s bytes" % len(m.group(0)))
            self.stream += m.group(0)
            try:
                decompressed = self.dc.decompress(m.group(0))
                if self.verbose > 2:
                    print("Decompressed %r" % decompressed)
                self.decompressed += decompressed
            except zlib.error as e:
                self.ERROR("Unable to decompress stream %s after %s bytes"
                           % (e, self.processed))
        else:
            self.stream += m.group(0)

    def END_STREAM(self, t, m):
        if self.state == 'FLATE_STREAM':
            try:
                ex = self.dc.decompress(b'Z') + self.dc.flush()
                if ex:
                    self.decompressed += ex
            except zlib.error as e:
                self.ERROR("Unable to decompress stream %s after %s bytes"
                           % (e, self.processed))

    def START_XREF(self, t, m):
        try:
            offset = int(m.group(0))
            if offset != 0:
                self.pdf.xref_offsets.append(offset)
        except (AttributeError, ValueError):
            pass
