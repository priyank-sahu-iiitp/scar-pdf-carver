# Cohen PDF Carver — Line-by-Line Notes

A fragmented PDF carver that recovers PDFs from a raw disk image (`.dd`) even when
the file's bytes are scattered across non-contiguous disk sectors.

**Core idea.** A PDF's own **xref table** lists every object's byte offset *inside
the file*. On disk, a fragmented file's bytes live at different physical offsets,
but every fragment is **sector-aligned (512 bytes)**. So if the xref says an
object lives at file-offset `O`, on disk it must sit at some offset where
`disk_offset % 512 == O % 512`. The carver searches the disk for candidate object
headers with that alignment, verifies them, builds a map `file_offset →
disk_offset`, and a virtual file (`Reassembler`) makes the fragments look
contiguous to a tolerant PDF parser.

Pipeline:
1. **Index** — scan disk for `N N obj` / `xref` / `startxref` byte positions.
2. **Maps** — parse each xref table, match its entries to disk hits by alignment,
   write `.map` files.
3. **Force** — read the disk through a `Reassembler` loaded with the map; resolve
   ambiguous sectors by trial parsing; write the final `.pdf`.

---

## `lexer.py` — generic streaming, state-machine tokenizer

```python
import re
import sys
```
Stdlib only. `re` drives all matching; `sys` imported but unused.

```python
class Lexer:
    tokens = []
    buffer = b''
    error = 0
    verbose = 0
    state_stack = []
    processed = 0
    processed_buffer = b''
    saved_state = None
```
Class-level defaults shared by all subclasses.
- `tokens` — token table filled by subclass (rows of `[state_re, content_re, handlers, next_state]`).
- `buffer` — unconsumed input bytes.
- `error` — running error count; the carver tolerates corruption by counting errors instead of crashing.
- `verbose` — debug level (0=silent, higher=more prints).
- `state_stack` — stack for nested constructs (arrays, dicts, streams).
- `processed` — total bytes consumed so far (absolute counter).
- `processed_buffer` — bytes consumed but not yet flushed to output.
- `saved_state` — snapshot for fast-recovery trial parsing.

```python
def __init__(self, verbose=0):
    if not self.verbose:
        self.verbose = verbose
    for row in self.tokens:
        row.append(re.compile(row[0], re.DOTALL))
        content_pat = row[1]
        if isinstance(content_pat, str):
            content_pat = content_pat.encode('latin-1')
        row.append(re.compile(content_pat, re.DOTALL))
```
Compiles the token table once. Each row gains two extra columns: a compiled
state-regex (col 4) and a compiled content-regex (col 5). `re.DOTALL` makes `.`
match newlines too. `latin-1` encodes any byte 0-255 1:1 so we can safely mix
str/bytes patterns.

```python
def save_state(self, t=None, m=None):
    if self.error:
        return
    if self.verbose > 2:
        print("Saving state %s\n" % self.processed)
    try:
        end = m.end()
    except AttributeError:
        end = 0
    self.saved_state = dict(
        state_stack=self.state_stack[:],
        processed=self.processed - end,
        processed_buffer=self.processed_buffer,
        readptr=self.fd.tell() - len(self.buffer) - end,
        state=self.state,
        objects=self.objects[:],
        error=self.error,
    )
```
Snapshots the parser at the *start* of the triggering token (subtract `m.end()`
so restore rewinds past the matched bytes). `readptr` reconstructs the true
underlying-file position: `fd.tell()` minus what's buffered minus the token.
Lists are sliced (`[:]`) to copy. Skipped when errors are already present
(corrupted state isn't worth snapshotting).

```python
def restore_state(self):
    if self.verbose > 2:
        print("Restoring state")
    state = self.saved_state
    if not state:
        return
    self.state_stack = state['state_stack']
    self.processed = state['processed']
    self.processed_buffer = state['processed_buffer']
    self.buffer = b''
    self.fd.seek(state['readptr'])
    self.state = state['state']
    self.objects = state['objects']
    self.error = state['error']
```
Inverse of `save_state`. `self.buffer = b''` throws away stale buffered bytes so
the next `feed()` pulls fresh data from the rewound file position. This is what
makes "what if this sector maps here?" trial parsing fast.

```python
def next_token(self, end=True):
    current_state = self.state
    for state_re, re_str, token, next_state_val, state, regex in self.tokens:
        if state.match(current_state):
```
The lexer's beating heart. Try every rule in order; the first whose **state
pattern** matches the current state, and whose **content regex** matches the
buffer, wins. Order = priority.

```python
            if self.verbose > 2:
                print("Trying to match %r with %r" % (self.buffer[:10], re_str))
            m = regex.match(self.buffer)
            if m:
                if self.verbose > 3:
                    print("%s matched %s" % (re_str, m.group(0)))
                self.processed_buffer += self.buffer[:m.end()]
                self.buffer = self.buffer[m.end():]
                self.processed += m.end()
```
Match at the **start** of the buffer. On hit, transfer the matched bytes from
`buffer` to `processed_buffer` and advance counters.

```python
                for t in token.split(','):
                    try:
                        if self.verbose > 0:
                            print("0x%X: Calling %s %r" % (self.processed, t, m.group(0)))
                        cb = getattr(self, t, self.default_handler)
                    except AttributeError:
                        continue
                    new_state = cb(t, m)
                    if new_state == "CONTINUE":
                        continue
                    elif new_state:
                        next_state_val = new_state
                        self.state = next_state_val
```
Token names can be comma-separated (e.g. `'save_state,PUSH_STATE,OBJECT_DECLERATION_START'`)
— each becomes a method call on `self`. If a callback returns a new state string,
adopt it. `"CONTINUE"` skips remaining callbacks for this match. The
`AttributeError` clause is effectively dead because `getattr` has a default.

```python
                if next_state_val:
                    self.state = next_state_val
                return token
```
Apply the rule's declared next state (if any), and return the token name to the
caller.

```python
    if (end and len(self.buffer) > 0) or len(self.buffer) > 1024:
        self.processed_buffer += self.buffer[:1]
        self.buffer = self.buffer[1:]
        self.ERROR("Lexer Stuck, discarding 1 byte (%r) - state %s"
                   % (self.buffer[:10], self.state))
        return "ERROR"
    return None
```
**No rule matched.** If we're at end-of-input with leftover bytes, *or* the
buffer is over 1024 bytes (definitely stuck), drop one byte and count an error.
This single-byte slip is what lets the carver survive garbage bytes without
halting. Otherwise return `None` meaning "need more data."

```python
def feed(self, data):
    self.buffer += data

def empty(self):
    return not len(self.buffer)
```
Append bytes; check empty.

```python
def default_handler(self, token, match):
    if self.verbose > 2:
        print("Default handler: %s with %r" % (token, match.group(0)))
```
No-op fallback for token names that don't have a dedicated method.

```python
def ERROR(self, message=None, weight=1):
    if message:
        print("Error(%s): %s" % (weight, message))
    self.error += weight
```
Error logger; `weight` lets a single bad event count for more.

```python
def PUSH_STATE(self, token=None, match=None):
    if self.verbose > 1:
        print("Storing state %s" % self.state)
    self.state_stack.append(self.state)

def POP_STATE(self, token=None, match=None):
    try:
        state = self.state_stack.pop()
        if self.verbose > 1:
            print("Returned state to %s" % state)
        return state
    except IndexError:
        print("Tried to pop the state but failed - possible recursion error")
        return None
```
Stack discipline for nested grammar (arrays, dicts, streams). `POP_STATE` returns
the popped state — `next_token` treats that as the new state.

```python
class SelfFeederMixIn(Lexer):
    def next_token(self):
        if len(self.buffer) < 512:
            self.feed()
        return Lexer.next_token(self)

    def feed(self, size=512):
        data = self.fd.read(size)
        Lexer.feed(self, data)
        return len(data)
```
Auto-pulls 512 bytes from `self.fd` whenever the buffer dips below 512. With
this mixin the parser becomes self-driving.

---

## `PDF.py` — PDF grammar on top of the lexer

```python
import zlib
import os
import lexer
```
`zlib` for decompressing `/FlateDecode` streams. `os` imported but unused.

```python
class PDFObject:
    def __init__(self, object_number, generation_number):
        self.object_number = object_number
        self.generation_number = generation_number
        self.contents = []
        self.stream = b''
        self.dictionary = {}
        self.decoded_stream = b''
```
One PDF indirect object (`N G obj ... endobj`). `contents` is the parsed value
list; `stream`/`decoded_stream` are raw and decompressed stream bytes;
`dictionary` is the object's `<< >>` if present (used to pick the stream filter).

```python
    def append(self, value):
        self.contents.append(value)
        if type(value) == dict:
            self.dictionary = value
```
Adds to `contents`; if the value is a dict, also stash it as `self.dictionary`
so the stream handler can read `/Filter` from it.

```python
    def __str__(self):
        return "Object %s %s, contains: %r, %r" % (
            self.object_number, self.generation_number,
            self.contents, self.decoded_stream[:100])
```
Debug representation.

```python
class PDFFile:
    def __init__(self):
        self.trailer = []
        self.xref = []
        self.xref_offsets = []
        self.objects = []
        self.xref_range = [0, 0]
```
A reconstructed PDF.
- `trailer` — trailer dict's contents.
- `xref` — list of `[obj_num, offset, gen, type]`.
- `xref_offsets` — other xref tables referenced via `/Prev` or `startxref` (incremental updates chain through these).
- `objects` — completed `PDFObject`s.
- `xref_range` — `[first_obj, last_obj]` numbers covered.

```python
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
```
Dump used as the `#`-prefixed header in `.map` files (so a map self-documents).

```python
class PDFParser(lexer.SelfFeederMixIn, lexer.Lexer):
    state = "INITIAL"
    error = 0
    verbose = 1
```
Inherits the auto-feeding lexer. Starts in `INITIAL`.

### Token table (the PDF grammar)

```python
['OBJECT DECLERATION', r'^stream[\r\n]+', 'PUSH_STATE,STREAM_START', None],
```
Inside an object, `stream` + newline → push state, decide flate vs generic via
`STREAM_START` callback (which returns the new state).

```python
['.*_STREAM', r'[\r\n]*endstream', 'END_STREAM,POP_STATE', None],
['.*_STREAM', r'.{1,200}?(?=[\r\n]*endstream)', 'STREAM', None],
['.*_STREAM', r'.{1,200}', 'STREAM', None],
```
In any `*_STREAM` state: `endstream` ends it. Otherwise consume up to 200 bytes
at a time. The middle rule's lookahead stops right before `endstream` so the
final chunk isn't oversized. Chunking keeps the buffer bounded.

```python
['.', r'%%EOF[\r\n]+', 'RESET_STATE', 'INITIAL'],
['.', r'%[^\n\r]*', 'COMMENT', None],
['.', r'(\r|\r\n|\n)$', 'EOL', None],
```
`%%EOF` ends a PDF. `%...` line is a comment. EOL match (anchored `$` requires
DOTALL semantics — practically benign here).

```python
['INITIAL', r'^(\d+) (\d+) obj',
 'save_state,PUSH_STATE,OBJECT_DECLERATION_START', 'OBJECT DECLERATION'],
['.', r'^endobj', 'OBJECT_DECLERATION_END,POP_STATE', None],
```
Object boundaries. `save_state` here means **every object boundary becomes a
fast-recovery anchor** — trial parses can rewind to the last object instead of
the file start.

```python
['.', r'^xref', 'PUSH_STATE', 'XREF_HEADER'],
['XREF_HEADER', r'(\d+) (\d+)', 'XREF_START', 'XREF_DATA'],
['.', r'^startxref', 'PUSH_STATE', 'START XREF'],
['START XREF', r'\d+', 'START_XREF,POP_STATE', None],
['XREF_DATA', r'(\d{10}) (\d{5}) ([a-zA-Z])', 'XREF_DATA', None],
```
Xref grammar: `xref` keyword → `<first> <count>` header → rows of exact form
`0000000000 00000 n` (10-digit offset, 5-digit generation, type char `n`/`f`).
`startxref <N>` records the byte offset of the master xref.

```python
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
```
All standard PDF value types. Literal strings have a tiny sub-grammar so `\(`
and `\)` don't end them. Names start with `/`. Whitespace is silently skipped.

### `__init__` and callbacks

```python
self.objects = []
self.fd = fd
lexer.Lexer.__init__(self)
self.pdf = PDFFile()
self.string = b''
self.xref_start_entry = 0
self.xref_entries_to_go = 0
self.verbose = verbose
```
Per-instance state. `objects` is the *parsing stack* (current dict/array/object
being built). `pdf` is the *result*. `string` accumulates literal-string bytes
between `(` and `)`. `xref_start_entry/_to_go` track xref-row enumeration.

```python
def ERROR(self, message=None):
    if message:
        print("Error: %s" % message)
    lexer.Lexer.ERROR(self)
```
Override that always prints, then delegates.

```python
def RESET_STATE(self, t, m):
    self.objects = []
```
On `%%EOF`, drop in-progress objects.

```python
def OBJECT_DECLERATION_START(self, t, m):
    self.add_to_current_object(PDFObject(m.group(1), m.group(2)))
```
On `N G obj`, push a new `PDFObject` onto the parsing stack.

```python
def OBJECT_DECLERATION_END(self, token, match):
    if not self.objects:
        return
    o = self.objects.pop()
    self.pdf.objects.append(o)
```
On `endobj`, move the object from the parsing stack to the finished list.

```python
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
```
The polymorphic "append a value to whatever's on top of the parsing stack":
- Empty stack → the value becomes the stack top.
- Has `.append` (it's a `PDFObject` or a list/array) → use it.
- Otherwise it's a dict using the `{1: <pending_key>}` convention: slot `1`
  holds the next key waiting for its value. If slot `1` is set → write
  `dict[key]=value`, clear slot. Else → this value becomes the next key.
- Trailer-`/Prev` special case: the value is another xref offset; also record it
  in `pdf.xref_offsets`.
- Outer `try/except Exception: pass` — never crash on garbage; just drop it.

```python
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
```
Straightforward value handlers. Note dicts begin with `{1: 0}` so the
pending-key sentinel always exists; `DICTIONARY_END` strips it before exposing
the dict. Hex strings are decoded pair-by-pair; bad pairs (e.g. whitespace
embedded) are silently skipped because `int(...,16)` will raise and be caught.

```python
def XREF_START(self, t, m):
    self.xref_start_entry = int(m.group(1))
    self.xref_entries_to_go = int(m.group(2))
    self.pdf.xref_range = [
        self.xref_start_entry,
        self.xref_start_entry + self.xref_entries_to_go
    ]
```
Read `<first> <count>` of an xref subsection. Sets the range used later for
matching incremental-update xref pieces.

```python
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
```
One xref row. Records `[obj, offset, gen, type]`. When the subsection is
exhausted, leave xref mode by popping state.

```python
def TRAILER(self, t, m):
    self.objects.append(self.pdf.trailer)
```
Push the result's trailer list onto the parsing stack so trailer contents collect
into it directly.

```python
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
```
At `stream` keyword, inspect the parent object's dict. If `/Filter` is
`/FlateDecode`, set up an incremental zlib decompressor and enter `FLATE_STREAM`;
otherwise `GENERIC_STREAM`. Returning the state string makes the lexer adopt it.

```python
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
```
Per chunk: accumulate raw bytes; if flate, also feed incremental decompression.
Decompression failure is counted as an error (suggests the wrong fragment was
chosen) but doesn't halt parsing.

```python
def END_STREAM(self, t, m):
    if self.state == 'FLATE_STREAM':
        try:
            ex = self.dc.decompress(b'Z') + self.dc.flush()
            if ex:
                self.decompressed += ex
        except zlib.error as e:
            self.ERROR(...)
```
Flush trailing bytes from the zlib state machine. Passing one extra dummy byte
+ `flush()` is a common trick to force any pending output out.

```python
def START_XREF(self, t, m):
    try:
        offset = int(m.group(0))
        if offset != 0:
            self.pdf.xref_offsets.append(offset)
    except (AttributeError, ValueError):
        pass
```
On `startxref <N>`, record `N` as a known xref offset (useful when chaining
incremental updates).

---

## `Carver.py` — `Reassembler`, a virtual file made of fragments

```python
import bisect
```
For maintaining a sorted list of map points.

```python
class Reassembler:
    interpolate_forward = True
    overread = 100 * 1024
```
Defaults: interpolation direction tiebreaker; how far past the last anchor
reading is allowed (100 KB).

```python
def __init__(self, fd):
    self.fd = fd
    self.points = []
    self.mapping = {}
    self.readptr = 0
```
Wraps a real file `fd`. `points` is a sorted list of *virtual file offsets*;
`mapping[virtual] = physical` (where on disk that virtual offset lives).
`readptr` is the current virtual position.

```python
def del_point(self, file_pos):
    try:
        idx = self.points.index(file_pos)
        del self.mapping[file_pos]
        self.points.pop(idx)
    except (ValueError, KeyError):
        pass
```
Remove an anchor (used when a tentative point turns out to produce errors).

```python
def add_point(self, file_pos, image_pos):
    bisect.insort_left(self.points, file_pos)
    if file_pos in self.mapping:
        expected, left = self.interpolate(file_pos - 1)
        if abs(image_pos - expected) > abs(self.mapping[file_pos] - expected):
            return
    self.mapping[file_pos] = image_pos
```
Insert anchor in sorted order. If a conflicting point already exists, **keep
whichever is closer to interpolation** — the carver resists outliers that
contradict the trend established by neighboring anchors.

```python
def seek(self, offset, whence=0):
    if whence == 0:
        self.readptr = offset
    elif whence == 1:
        self.readptr += offset
```
Standard file seek (no `whence=2` end-relative).

```python
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
```
**Translate virtual → physical.** Between two anchors the data is assumed
contiguous, so the disk offset is `anchor_disk + (virtual_offset -
anchor_virtual)`. Forward: anchor on the left. Backward: anchor on the right.
Outside the anchor range, force the only sensible direction. `left` = bytes
until the next anchor — callers must re-interpolate after that.

```python
def tell(self):
    return self.readptr

def read(self, length):
    result = b''
    while length > 0:
        try:
            m, left = self.interpolate(self.readptr)
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
        result += data
        length -= len(data)
    return result
```
The whole point: `read()` translates the current virtual pointer, seeks the real
file, reads up to the next anchor, advances. `Reassembler` looks like a normal
file to the parser but stitches fragments together transparently. If
interpolation fails (no points yet), best-effort `left=1000`. The `if
want_to_read <= 0` guard prevents an infinite loop if `interpolate` returns a
non-positive span.

```python
def load_map(self, mapfile):
    with open(mapfile, 'r') as fd:
        for line in fd:
            if line.startswith("#"):
                continue
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            self.add_point(int(parts[0]), int(parts[1]))
```
Reads `.map` files. `#` lines are the `PDFFile.__str__` header (self-doc).
Data lines: `virtual_offset physical_offset [optional_label]`.

---

## `pdf_carver.py` — stages 1 & 2

```python
#!/usr/bin/env python3
from optparse import OptionParser
import re, sys, pickle
import PDF
from Carver import Reassembler

SECTOR_SIZE = 512
```
Sector size is the linchpin of the whole alignment trick.

```python
parser = OptionParser(usage="""%prog """)
parser.add_option('-i', '--index', default=None, help='Index file to operate on')
parser.add_option('-c', '--create', default=False, action="store_true", help='Create a new index file')
parser.add_option('-m', '--maps', default=False, action="store_true", help='Carve the index file by creating initial map files')
parser.add_option('-p', '--print', default=False, action="store_true", help='Print the index hits')
(options, args) = parser.parse_args()

if not options.index:
    print("Need an index file to operate on.")
    sys.exit(1)
```
Three modes: `-c` create index, `-m` make maps, `-p` print. Index file path is
mandatory.

```python
regexs = {
    'OBJECTS':   rb'(\d+) (\d+) obj',
    'XREFS':     rb'([\r\n ])xref([\r\n])',
    'STARTXREF': rb'startxref([\r\n])',
}
cregexs = {}
hits = {}
```
Three patterns to scan disk for. `XREFS` includes leading whitespace — that's
why later code uses `+1` for the sector remainder.

```python
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
```
Stage 1. Stream the raw disk in 400 KB blocks; for each block, record absolute
byte offsets of every regex hit. Save the dict with `pickle`. Minor cost: a
match straddling a block boundary is missed — acceptable for carving since most
matches occur many times.

```python
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
```
**The alignment trick.** For each in-use xref entry (`typ == 'n'`):
- The object's virtual offset is `offset`.
- Filter all disk `OBJECTS` hits to those with the same `% 512` remainder.
- Verify each by seeking and reading the line — only keep ones whose header
  actually matches `<id> <gen>`.
- Write one or more candidate mapping lines.
The `#`-prefixed `pdf.__str__` dump goes at the top so the map is
self-documenting.

```python
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
```
Stage 2 driver. For every xref offset in the index: seek and parse until
`%%EOF` (or 10 errors), then record the resulting `PDFFile` and write its map.

```python
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
```
**Chain incremental updates.** A PDF can have multiple xref sections (modify
& append). One xref's `/Prev` (collected in `xref_offsets`) points at another's
location. We match by:
- Sector alignment (`xref_offset % 512 == y % 512 + 1`, +1 because the
  `XREFS` regex's leading whitespace shifts position by one).
- Adjacent object-number ranges (`y.range[0] == x.range[1]` or vice versa).
On match, synthesize a combined `PDFFile` and write `x-y.map`. This is the
origin of files like `6179955-6133887.map`.

```python
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
```
Mode dispatch. `getattr(options, 'print')` because `print` shadows the builtin.

---

## `PDFForcer.py` — stage 3, force-reconstruct the actual PDF

```python
#!/usr/bin/env python3
import sys
from optparse import OptionParser
import Carver
import PDF
```

```python
def check_errors(p, end_offset):
    p.restore_state()
    while p.processed < end_offset and p.error < 10:
        p.next_token()
    return p.error
```
Trial-parse helper: rewind to the last snapshot, parse forward up to
`end_offset` (or 10 errors), return error count. **Low errors ⇒ the current map
arrangement parses cleanly ⇒ it's probably right.**

```python
parser.add_option('-m', '--map', ...)            # required
parser.add_option('-r', '--reconstructed', ...)  # optional dump of final map
parser.add_option('-o', '--output', ...)         # required output PDF
parser.add_option('-v', '--verbose', ..., type='int')
parser.add_option('-s', '--slow', action="store_true", ...)  # disable fast recovery
```
Options. `-s` slow mode reparses from offset 0 for every trial instead of
using `save_state` recovery — slower but more conservative.

```python
c = Carver.Reassembler(open(args[0], 'rb'))
p = PDF.PDFParser(c)
p.verbose = options.verbose
c.load_map(options.map)
```
Wrap the disk image in a `Reassembler`, load the candidate map, hand the
`Reassembler` to a `PDFParser` as its "file."

```python
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
```
**Ambiguity resolution.** Walk every 512-byte sector of the virtual file. Where
forward and backward interpolation **disagree**, the sector is ambiguous (one
fragment ended, another started). Try anchoring it to the reverse-interpolated
disk offset, parse forward ~4 KB beyond the next anchor, and **keep the point
only if the trial run produced zero errors**. Bad guesses are removed. This is
the carver iteratively "trying" fragment arrangements.

```python
if options.reconstructed:
    sys.stderr.write("Writing reconstructed file: %s\n" % options.reconstructed)
    with open(options.reconstructed, 'w') as fd:
        for x in c.points:
            fd.write("%s %s\n" % (x, c.mapping[x]))
```
Optionally save the *resolved* map for inspection.

```python
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
```
**Final emit.** Fresh parser; read from offset 0 through the `Reassembler`;
flush every token's consumed bytes (`processed_buffer`) to the output file.
That stream of consumed bytes **is** the reconstructed contiguous PDF. Stop at
`%%EOF` past the last map anchor, or when input is exhausted. Final error
count printed.

---

## `carve_pdf.py` — one-command orchestrator

```python
#!/usr/bin/env python3
"""Carve PDFs from a raw disk image.

Usage:
  python carve_pdf.py disk.dd --out recovered/
"""
import argparse, os, subprocess, sys
from pathlib import Path

_HERE = Path(__file__).parent
_PY = sys.executable
```
`_HERE` finds sibling scripts (so the script works from any cwd). `_PY` uses
the current interpreter so virtualenvs work.

```python
def _run(cmd, cwd, label):
    print(f"[{label}]", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout, end="")
    if r.returncode != 0:
        print(r.stderr, end="", file=sys.stderr)
        sys.exit(f"FAILED at: {label}")
    return r
```
Subprocess runner that bails on nonzero exit. Stdout is echoed; stderr only on
failure.

```python
def main():
    ap = argparse.ArgumentParser(description="Carve PDFs from a raw disk image.")
    ap.add_argument("disk", help="Disk image (.dd / raw)")
    ap.add_argument("--out", default="carved_pdfs", help="Output directory")
    args = ap.parse_args()

    disk = os.path.abspath(args.disk)
    out = os.path.abspath(args.out)

    if not os.path.exists(disk):
        sys.exit(f"ERROR: not found: {disk}")
    os.makedirs(out, exist_ok=True)

    idx = os.path.join(out, "index.idx")

    _run([_PY, _HERE / "pdf_carver.py", "-i", idx, "-c", disk], out, "1/3 build index")
    _run([_PY, _HERE / "pdf_carver.py", "-i", idx, "-m", disk], out, "2/3 generate maps")
```
Set up paths; run stages 1 and 2. Both stages must succeed (`_run` aborts on
failure).

```python
    maps = list(Path(out).glob("*.map"))
    if not maps:
        sys.exit("No .map files generated — no recoverable PDFs found.")
    ok, failed = 0, 0
    for m in sorted(maps):
        pdf = m.with_suffix(".pdf")
        r = subprocess.run(
            [_PY, _HERE / "PDFForcer.py", "-m", m.name, "-o", pdf.name, disk],
            cwd=out, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  SKIP {m.name}: carver exited {r.returncode}", file=sys.stderr)
            failed += 1
        else:
            size = pdf.stat().st_size if pdf.exists() else 0
            print(f"  {pdf.name}  {size} bytes")
            ok += 1
    print(f"\nDone. {ok} PDF(s) recovered, {failed} skipped → {out}/")
```
Stage 3: per-map carving. **Doesn't** use `_run` because per-map failure should
be skipped, not abort the whole batch. Final summary count.

```python
if __name__ == "__main__":
    main()
```

---

## End-to-end recap

| Stage | Script | Input | Output | Key trick |
|---|---|---|---|---|
| 1 | `pdf_carver.py -c` | `disk.dd` | `index.idx` (pickle) | regex scan for `obj` / `xref` / `startxref` |
| 2 | `pdf_carver.py -m` | index + disk | `*.map` files | parse xref; match objects to disk by **`% 512`** sector alignment |
| 3 | `PDFForcer.py` | `.map` + disk | `.pdf` | `Reassembler` virtual file + **trial parsing** to resolve ambiguous sectors |

The brilliance is using the PDF's **own xref table as ground truth**, sector
alignment to narrow the candidate space, and error-counted trial parsing
to choose between candidates. `lexer.py` + `PDF.py` are a corruption-tolerant
parser; `Carver.py` is the fragment-stitching file abstraction; `PDFForcer.py`
ties them together.
