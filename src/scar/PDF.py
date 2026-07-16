import zlib
import os
import lexer

# Ablation switch (C1): when True, PDFParser treats /Type /XRef objects as
# opaque streams instead of decoding them, reproducing the pre-C1 (classic
# xref table only) capability for evaluation. Default False: normal behavior.
DISABLE_XREF_STREAM = False


class PDFObject:
    # Represents a single PDF object with its contents and stream
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
    # Holds parsed xref table, objects, and trailer for one PDF
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
    # Lexer-based PDF parser; reads from a file-like object (fd)
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
        # Decode PDF 1.5+ /Type /XRef compressed cross-reference streams
        # (skipped when DISABLE_XREF_STREAM is set, for C1 ablation)
        try:
            if (not DISABLE_XREF_STREAM
                    and getattr(o, 'dictionary', {}).get(b'/Type') == b'/XRef'):
                self.parse_xref_stream(o)
        except Exception:
            pass

    @staticmethod
    def _png_unpredict(raw, cols):
        # Reverse PNG row predictors (filter bytes 0-4) used in xref streams
        stride = cols + 1
        out = bytearray()
        prev = bytearray(cols)
        for i in range(0, len(raw), stride):
            if i + stride > len(raw):
                break
            ftype = raw[i]
            line = bytearray(raw[i + 1:i + 1 + cols])
            if ftype == 0:
                pass
            elif ftype == 1:
                for j in range(len(line)):
                    left = line[j - 1] if j > 0 else 0
                    line[j] = (line[j] + left) & 0xFF
            elif ftype == 2:
                for j in range(len(line)):
                    line[j] = (line[j] + prev[j]) & 0xFF
            elif ftype == 3:
                for j in range(len(line)):
                    left = line[j - 1] if j > 0 else 0
                    line[j] = (line[j] + ((left + prev[j]) >> 1)) & 0xFF
            elif ftype == 4:
                for j in range(len(line)):
                    left = line[j - 1] if j > 0 else 0
                    up = prev[j]
                    upleft = prev[j - 1] if j > 0 else 0
                    pp = left + up - upleft
                    pa, pb, pc = abs(pp - left), abs(pp - up), abs(pp - upleft)
                    if pa <= pb and pa <= pc:
                        pred = left
                    elif pb <= pc:
                        pred = up
                    else:
                        pred = upleft
                    line[j] = (line[j] + pred) & 0xFF
            out.extend(line)
            prev = line
        return bytes(out)

    # Decode a /Type /XRef compressed xref stream into self.pdf.xref
    def parse_xref_stream(self, obj):
        d = getattr(obj, 'dictionary', None)
        if not isinstance(d, dict) or d.get(b'/Type') != b'/XRef':
            return False

        W = d.get(b'/W')
        if not isinstance(W, list) or len(W) != 3:
            return False
        try:
            w1, w2, w3 = int(W[0]), int(W[1]), int(W[2])
        except (TypeError, ValueError):
            return False
        row = w1 + w2 + w3
        if row <= 0:
            return False

        stream = getattr(obj, 'stream', b'')
        if not stream:
            return False
        try:
            raw = zlib.decompress(stream)
        except zlib.error:
            return False

        params = d.get(b'/DecodeParms')
        if not isinstance(params, dict):
            params = {}
        try:
            predictor = int(params.get(b'/Predictor', 1))
        except (TypeError, ValueError):
            predictor = 1
        if predictor >= 10:
            try:
                cols = int(params.get(b'/Columns', row))
            except (TypeError, ValueError):
                cols = row
            if cols <= 0:
                cols = row
            raw = self._png_unpredict(raw, cols)
        elif predictor == 2:
            return False

        try:
            size = int(d.get(b'/Size', 0))
        except (TypeError, ValueError):
            size = 0
        idx = d.get(b'/Index')
        if isinstance(idx, list) and len(idx) >= 2 and len(idx) % 2 == 0:
            try:
                ranges = [(int(idx[i]), int(idx[i + 1]))
                          for i in range(0, len(idx), 2)]
            except (TypeError, ValueError):
                ranges = [(0, size)]
        else:
            ranges = [(0, size)]

        added = 0
        pos = 0
        for first, count in ranges:
            for k in range(count):
                if pos + row > len(raw):
                    break
                entry = raw[pos:pos + row]
                pos += row
                t = int.from_bytes(entry[:w1], 'big') if w1 else 1
                f2 = int.from_bytes(entry[w1:w1 + w2], 'big')
                f3 = int.from_bytes(entry[w1 + w2:w1 + w2 + w3], 'big') if w3 else 0
                obj_num = first + k
                if t == 1:
                    self.pdf.xref.append([obj_num, f2, f3, 'n'])
                    added += 1
                elif t == 0:
                    self.pdf.xref.append([obj_num, f2, f3, 'f'])
                elif t == 2:
                    # 's' = stored in object stream; f2=ObjStm obj#, f3=index
                    self.pdf.xref.append([obj_num, f2, f3, 's'])
        if added:
            nums = [e[0] for e in self.pdf.xref]
            self.pdf.xref_range = [min(nums), max(nums) + 1]
        return added > 0

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
            self.stream = bytearray()
            return "GENERIC_STREAM"

        is_flate = obj.dictionary.get(b'/Filter') == b'/FlateDecode'
        length = obj.dictionary.get(b'/Length')

        # Fast path: use /Length to consume stream in one read instead of chunking
        if isinstance(length, (int, float)) and length > 0:
            n = int(length)
            need = n - len(self.buffer)
            if need > 0:
                self.buffer.extend(self.fd.read(need))
            if len(self.buffer) >= n:
                raw = bytes(self.buffer[:n])
                del self.buffer[:n]
                self.processed += n
                self.processed_buffer += raw
                self.stream = raw
                self.decompressed = b''
                if is_flate:
                    try:
                        self.decompressed = zlib.decompress(raw)
                    except zlib.error as e:
                        self.ERROR("Unable to decompress stream %s after "
                                   "%s bytes" % (e, self.processed))
                return "GENERIC_STREAM"

        if is_flate:
            self.dc = zlib.decompressobj()
            # Use bytearray for stream accumulation to avoid O(n^2) copies
            self.stream = bytearray()
            self.decompressed = bytearray()
            return "FLATE_STREAM"
        else:
            self.stream = bytearray()
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
        # Attach raw and decoded stream to the object for downstream verification
        obj = self.objects[-1] if self.objects else None
        if hasattr(obj, 'stream'):
            obj.stream = bytes(self.stream)
            obj.decoded_stream = bytes(getattr(self, 'decompressed', b''))

    def START_XREF(self, t, m):
        try:
            offset = int(m.group(0))
            if offset != 0:
                self.pdf.xref_offsets.append(offset)
        except (AttributeError, ValueError):
            pass
