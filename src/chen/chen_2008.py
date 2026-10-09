"""
chen_2008.py -- Implementation of the Chen et al. (2008) PDF carving method.

Paper
-----
Chen, M.; Zheng, N.; Xu, M.; Lou, Y.; Wang, X. (2008).
"Validation Algorithms Based on Content Characters and Internal Structure:
The PDF File Carving Method." 2008 International Symposium on Information
Science and Engineering (ISISE), pp. 168-172. DOI 10.1109/ISISE.2008.209.

Only what the paper describes is implemented. Constants are the paper's own:
512-byte sectors (eqs 1.9, 1.10, 1.13), 256-byte entropy windows and the
entropy-difference threshold 0.8 (eq 1.12, Section IV.C).

Process (Figure 2)
------------------
  disk image
    -> A. header / file length / maximal offset / footer validation (IV.A)
         linearized:      f = h + l                       (eq 1.1), %%EOF at f
         non-linearized:  objM = h + offM                 (eq 1.2), object at objM
         -> intact PDF files are copied out
         -> the rest are "default fragmented"
    -> B. internal structure validation (IV.B)
         intact-object test   Len1 = Off2 - Off1, Len2 = Toff2 - Toff1, Len1 = Len2
                                                          (eqs 1.3-1.5)
         pretreatment: merge cross-reference sections     (eqs 1.6-1.7)
         seek O(i+1), O(i+2) after a fragmented object O(i)  (IV.B.2.b)
         fragN = fobjN + 1                                (eq 1.8)
         temporary boundaries fragH, fragF                (eqs 1.9-1.10)
    -> C. entropy difference validation (IV.C, eq 1.12)
         confirmed boundaries -> copied out
         unconfirmed          -> D (and E)
    -> D. zlib/deflate decompression validation (IV.D, steps 1-9, eq 1.13)
         confirmed boundaries -> copied out (step 9)
         otherwise the temporary boundaries are the final boundaries
    -> E. character table validation (IV.E) -- NOT IMPLEMENTED, see below.

Section IV.E (character table validation) is left out on purpose. The paper
says the method "establishes PDF characters table (It is small and needs to
test more dataset for adding more characters)" but does not publish the table.
Building one would mean inventing data the paper does not give, so the cascade
here runs A -> B -> C -> D only.

Reading of eq 1.13 (Section IV.D)
---------------------------------
For a fragmented stream object O(i) whose bytes start in fragment j and end in
fragment j+1, we read the symbols of eq 1.13 as
    objLen   = Off(i+1) - Off(i)                  length of O(i), from eq 1.3
    streamL1 = fragF(j)   - Toff(i)               bytes of O(i) inside fragment j,
                                                  up to its temporary foot (eq 1.10)
    streamL2 = Toff(i+1) - fragH(j+1)             bytes of O(i) inside fragment j+1,
                                                  from its temporary head (eq 1.9)
so streamSN = floor((objLen - streamL1 - streamL2) / 512) is the number of
sectors of O(i) that lie between the two temporary boundaries. Steps 5-7 start
with stream1 taking all of those sectors (the cut at the head of fragment j+1)
and, on each failed decompression, move one sector from the end of stream1 to
the head of stream2 ("stream1 subtracts the last sector, the head of stream2
augments one preceding contiguous sector"), until c surpasses streamSN. With
this reading the loop tries every sector-aligned cut between the two temporary
boundaries, which is the reading chosen where the paper is ambiguous.

Other interpretation choices, all documented at the point of use:
  * IV.C: a boundary counts as confirmed by entropy difference only when
    exactly one candidate sector boundary reaches HD >= 0.8; none or several
    leave it unconfirmed, and it passes to IV.D as in Figure 2.
  * IV.B: the first fragment's head is the PDF header h, and the last
    fragment's foot is the %%EOF footer that follows its last object (the
    footer of IV.A), because eq 1.10 alone would cut off the cross-reference
    table and trailer that follow the last object.

API
---
``run_all_algorithms(raw_data, image=None, start=0) -> dict[str, ChenResult]``
with keys ``A1_HFV, A2_ISV, A3_EDV, A4_ZDV, A5_PC``. ``raw_data`` is the
candidate region (from a %PDF- header). ``image``/``start`` give the whole disk
image and the region's absolute offset, needed because IV.B searches the whole
image for objects of later fragments. ``results["A5_PC"]`` is the cascade
output that downstream scripts consume.
"""

from __future__ import annotations

import io
import logging
import math
import re
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Paper-fixed constants
# ------------------------------------------------------------------
SECTOR_SIZE = 512    # eqs 1.9, 1.10, 1.13
ENTROPY_WIN = 256    # eq 1.12: n = m = 256
ENTROPY_TH = 0.8     # Section IV.C: "good result when the entropy difference value equals to 0.8"

# ------------------------------------------------------------------
# Regexes (PDF syntax, ISO 32000 / PDF Reference 1.7, cited by the paper as [9])
# ------------------------------------------------------------------
_HEADER_RE = re.compile(rb"%PDF-\d+\.\d+")
_OBJ_RE = re.compile(rb"(?<!\d)(\d{1,7})[ \t\r\n]+(\d{1,5})[ \t\r\n]+obj\b")
_XREF_KW_RE = re.compile(rb"(?<!start)xref[ \t]*(?:\r\n|\r|\n)")
_SUBSEC_RE = re.compile(rb"[ \t\r\n]*(\d+)[ \t]+(\d+)[ \t]*(?:\r\n|\r|\n)")
_ENTRY_RE = re.compile(rb"(\d{10})[ \t](\d{5})[ \t]([nf])")
_TRAILER_RE = re.compile(rb"[ \t\r\n]*trailer")
_SIZE_RE = re.compile(rb"/Size[ \t\r\n]+(\d+)")
_PREV_RE = re.compile(rb"/Prev[ \t\r\n]+(\d+)")
_LIN_RE = re.compile(rb"/Linearized\b")
_LIN_L_RE = re.compile(rb"/L[ \t\r\n]+(\d{1,12})")
_LEN_IND_RE = re.compile(rb"/Length[ \t\r\n]+(\d+)[ \t\r\n]+(\d+)[ \t\r\n]+R")
_LEN_DIR_RE = re.compile(rb"/Length[ \t\r\n]+(\d+)(?![ \t\r\n]+\d+[ \t\r\n]+R)")
_FLATE_RE = re.compile(rb"/FlateDecode\b")
_STREAM_RE = re.compile(rb"stream(?:\r\n|\n|\r)")
_EOF_RE = re.compile(rb"%%EOF")


@dataclass
class ChenResult:
    alg_used: Optional[str]
    pdf_bytes: Optional[bytes]
    renderable: bool
    page_count: Optional[int]
    n_objects: int
    elapsed_s: float
    notes: str = ""


# ==================================================================
# Helpers
# ==================================================================

def _try_pikepdf(pdf_bytes: bytes) -> Tuple[bool, Optional[int]]:
    """Return (renderable, page_count). Used only by the runner to decide
    whether to write a file, as in the evaluated version; not part of the paper."""
    if not pdf_bytes:
        return False, None
    try:
        import pikepdf  # type: ignore
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            return True, len(pdf.pages)
    except Exception:
        return False, None


def _count_objects(data: bytes) -> int:
    return len(_OBJ_RE.findall(data)) if data else 0


def _frag_head(pos: int) -> int:
    """fragH = floor(objF / 512) * 512    (eq 1.9)"""
    return (pos // SECTOR_SIZE) * SECTOR_SIZE


def _frag_foot(pos: int) -> int:
    """fragF = (floor(objL / 512) + 1) * 512    (eq 1.10)"""
    return (pos // SECTOR_SIZE + 1) * SECTOR_SIZE


def _entropy(buf: bytes) -> float:
    """H(x) = -sum p(i) log2 p(i)    (eq 1.11)"""
    if not buf:
        return 0.0
    n = len(buf)
    return -sum((c / n) * math.log2(c / n) for c in Counter(buf).values())


def _hd(image, b: int) -> float:
    """Entropy difference at sector boundary b (eq 1.12): last 256 bytes of the
    sector ending at b against the first 256 bytes of the next sector."""
    if b - ENTROPY_WIN < 0 or b + ENTROPY_WIN > len(image):
        return 0.0
    return abs(_entropy(bytes(image[b - ENTROPY_WIN:b])) - _entropy(bytes(image[b:b + ENTROPY_WIN])))


def _inflates(raw: bytes) -> bool:
    """True iff raw is one complete zlib (RFC 1950) or raw deflate (RFC 1951)
    stream -- the decompression test of Section IV.D, step 5."""
    if not raw:
        return False
    for wbits in (15, -15):
        try:
            d = zlib.decompressobj(wbits)
            d.decompress(raw)
            if d.eof:
                return True
        except zlib.error:
            continue
    return False


def _eof_after(image, pos: int, limit: int) -> int:
    """End of the first %%EOF footer after pos (Section IV.A footer), including
    its end-of-line marker, or -1."""
    m = _EOF_RE.search(image, pos, limit)
    if not m:
        return -1
    e = m.end()
    if image[e:e + 2] == b"\r\n":
        return e + 2
    if image[e:e + 1] in (b"\r", b"\n"):
        return e + 1
    return e


# ---- object-header index over the whole image (Section IV.B.2.b searches it) ----
_INDEX_CACHE: Dict[Tuple[int, int], Dict[Tuple[int, int], List[int]]] = {}


def _object_index(image) -> Dict[Tuple[int, int], List[int]]:
    key = (id(image), len(image))
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        idx = {}
        for m in _OBJ_RE.finditer(image):
            idx.setdefault((int(m.group(1)), int(m.group(2))), []).append(m.start())
        _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = idx
    return idx


def _obj_at(image, pos: int, num: int, gen: int) -> bool:
    """True iff the header `num gen obj` begins exactly at pos."""
    if pos < 0 or pos >= len(image):
        return False
    m = _OBJ_RE.match(image, pos, min(len(image), pos + 32))
    return bool(m) and int(m.group(1)) == num and int(m.group(2)) == gen


# ==================================================================
# Cross-reference tables (Section III; IV.A; IV.B.2.a)
# ==================================================================

@dataclass
class XrefSection:
    pos: int                                  # absolute image offset of "xref"
    end: int                                  # end of the trailer dictionary
    first: int                                # first object number of the first subsection
    count: int                                # total entries over all subsections
    entries: List[Tuple[int, int, int, bool]] = field(default_factory=list)
    size: Optional[int] = None                # trailer /Size
    prev: Optional[int] = None                # trailer /Prev


def _parse_xref(image, pos: int) -> Optional[XrefSection]:
    """Parse one cross-reference section at pos (Section III): subsections of
    "first count" lines followed by 20-byte entries "nnnnnnnnnn ggggg n|f EOL",
    then the trailer dictionary with /Size and /Prev. Any end-of-line marker
    (CR, LF or CR LF) is accepted, as PDF allows all three."""
    m = _XREF_KW_RE.match(image, pos)
    if not m:
        return None
    p = m.end()
    entries: List[Tuple[int, int, int, bool]] = []
    first_obj = None
    n = len(image)
    while p < n:
        sm = _SUBSEC_RE.match(image, p)
        if not sm:
            break
        first, count = int(sm.group(1)), int(sm.group(2))
        if count <= 0 or count > 10_000_000:
            break
        if first_obj is None:
            first_obj = first
        p = sm.end()
        for k in range(count):
            em = _ENTRY_RE.match(image, p)
            if not em:
                return None
            entries.append((first + k, int(em.group(1)), int(em.group(2)), em.group(3) == b"n"))
            p = em.end()
            while p < n and image[p:p + 1] in (b" ", b"\r", b"\n") and p - em.end() < 3:
                p += 1
    if not entries:
        return None
    sec = XrefSection(pos=pos, end=p, first=first_obj or 0, count=len(entries), entries=entries)
    tm = _TRAILER_RE.match(image, p)
    if tm:
        close = image.find(b">>", tm.end(), tm.end() + 4096)
        tail = bytes(image[tm.end(): close if close > 0 else tm.end() + 4096])
        s = _SIZE_RE.search(tail)
        pv = _PREV_RE.search(tail)
        sec.size = int(s.group(1)) if s else None
        sec.prev = int(pv.group(1)) if pv else None
        sec.end = (close + 2) if close > 0 else tm.end()
    return sec


def _scan_xref_sections(image, h: int, limit: int) -> List[XrefSection]:
    """Section IV.A, non-linearized files: "scans the data following the header
    to seek its cross-reference table" -- scan forward from the header for every
    "xref" keyword, not only the one named by startxref."""
    out = []
    for m in _XREF_KW_RE.finditer(image, h, limit):
        sec = _parse_xref(image, m.start())
        if sec:
            out.append(sec)
    return out


def _merge_sections(secs: List[XrefSection], h: int) -> Dict[int, Tuple[int, int, int, bool]]:
    """Section IV.B.2.a pretreatment: merge all cross-reference sections that
    belong to the same file. Two sections belong together if one names the
    other through /Prev (offsets are counted from the header h), or if they
    satisfy eqs 1.6-1.7: y1 = x2 and S = y1 + y2. Newer sections override older
    entries for the same object (incremental updates)."""
    if not secs:
        return {}
    group = {0}
    changed = True
    sizes = {s.size for s in secs if s.size}
    while changed:
        changed = False
        for i, a in enumerate(secs):
            if i in group:
                continue
            for j in list(group):
                b = secs[j]
                linked = (b.prev is not None and a.pos == h + b.prev) or \
                         (a.prev is not None and b.pos == h + a.prev)
                lo, hi = (a, b) if a.first <= b.first else (b, a)
                eq16 = lo.first == 0 and lo.count == hi.first                       # eq 1.6: y1 = x2
                eq17 = (lo.count + hi.count) in sizes                               # eq 1.7: S = y1 + y2
                if linked or (eq16 and eq17):
                    group.add(i)
                    changed = True
                    break
    merged: Dict[int, Tuple[int, int, int, bool]] = {}
    for k in sorted(group, key=lambda k: -secs[k].pos):      # newest (last in file) first
        for e in secs[k].entries:
            merged.setdefault(e[0], e)
    return merged


def _resolve_length(image, dict_bytes: bytes, entries: Dict[int, Tuple[int, int, int, bool]],
                    deltas: List[int], h: int) -> Optional[int]:
    """Stream /Length: a direct integer, or an indirect reference "N G R"
    resolved by reading object N (PDF Reference, cited by the paper as [9])."""
    m = _LEN_IND_RE.search(dict_bytes)
    if m:
        num, gen = int(m.group(1)), int(m.group(2))
        e = entries.get(num)
        cands = []
        if e and e[3]:
            cands = [h + d + e[1] for d in deltas]
        cands += _object_index(image).get((num, gen), [])
        for pos in cands:
            if _obj_at(image, pos, num, gen):
                om = _OBJ_RE.match(image, pos)
                vm = re.match(rb"[ \t\r\n]*(\d+)[ \t\r\n]+endobj", image[om.end():om.end() + 64])
                if vm:
                    return int(vm.group(1))
        return None
    m = _LEN_DIR_RE.search(dict_bytes)
    return int(m.group(1)) if m else None


# ==================================================================
# The cascade
# ==================================================================

@dataclass
class _Frag:
    objs: List[Tuple[int, int, int, int]]     # (num, gen, off, Toff) in xref order
    delta: int                                # Toff - (h + off) for every object of the fragment
    head: int = 0                             # temporary head (eq 1.9) or confirmed
    foot: int = 0                             # temporary foot (eq 1.10) or confirmed


def _hfv(image, h: int, limit: int):
    """Section IV.A. Returns (intact_bytes_or_None, note, sections, linearized)."""
    head = bytes(image[h:min(limit, h + 1024)])
    lin = _LIN_RE.search(head)
    secs = _scan_xref_sections(image, h, limit)
    if lin:
        lm = _LIN_L_RE.search(head, lin.end())
        if lm:
            f = h + int(lm.group(1))                                    # eq 1.1: f = h + l
            if f <= len(image) and _EOF_RE.search(image, max(h, f - 32), min(len(image), f + 8)):
                return bytes(image[h:f]), "linearized intact (eq 1.1)", secs, True
        return None, "linearized: no %%EOF at f = h + l (eq 1.1) -> default fragmented", secs, True
    entries = _merge_sections(secs, h)
    in_use = [(num, off, gen) for num, (num_, off, gen, used) in entries.items() if used and off > 0]
    if not in_use:
        return None, "no cross-reference table found below the header -> default fragmented", secs, False
    num, offM, gen = max(in_use, key=lambda e: e[1])
    objM = h + offM                                                     # eq 1.2: objM = h + offM
    if not _obj_at(image, objM, num, gen):
        return None, "no object at objM = h + offM (eq 1.2) -> default fragmented", secs, False
    newest = max(secs, key=lambda s: s.pos)
    end = _eof_after(image, newest.end, limit)
    if end < 0:
        return None, "no %%EOF after the cross-reference table -> default fragmented", secs, False
    return bytes(image[h:end]), "non-linearized intact (eq 1.2)", secs, False


def _isv(image, h: int, limit: int, secs: List[XrefSection], log: List[str]):
    """Section IV.B. Returns (fragments, fobjN, broken, entries)."""
    entries = _merge_sections(secs, h)                                  # IV.B.2.a
    objs = sorted([(num, gen, off) for num, (num_, off, gen, used) in entries.items() if used and off > 0],
                  key=lambda e: e[2])                                    # "arranged ... according to the offsets"
    if not objs:
        return [], 0, True, entries
    idx = _object_index(image)
    frags: List[_Frag] = []
    num, gen, off = objs[0]
    toff = h + off
    if not _obj_at(image, toff, num, gen):
        log.append("first object not below the header")
        return [], 0, True, entries
    cur = _Frag(objs=[(num, gen, off, toff)], delta=toff - (h + off))
    fobjN = 0
    broken = False
    i = 0
    while i < len(objs) - 1:
        n1, g1, o1, t1 = cur.objs[-1]
        n2, g2, o2 = objs[i + 1]
        len1 = o2 - o1                                                   # eq 1.3
        t2 = t1 + len1
        if _obj_at(image, t2, n2, g2):                                   # eq 1.4-1.5: Len2 == Len1
            cur.objs.append((n2, g2, o2, t2))
            i += 1
            continue
        # O(i) is fragmented: seek O(i+1) and O(i+2) anywhere in the image (IV.B.2.b),
        # accepting a candidate only if their spacing matches the xref offsets (eq 1.5).
        fobjN += 1
        frags.append(cur)
        cands = []
        if i + 2 < len(objs):
            n3, g3, o3 = objs[i + 2]
            for c in idx.get((n2, g2), []):
                if _obj_at(image, c + (o3 - o2), n3, g3):
                    cands.append(c)
        else:
            cands = list(idx.get((n2, g2), []))     # last object: no O(i+2) to test against
        cands = [c for c in cands if not any(c == t for f in frags for (_, _, _, t) in f.objs)]
        if not cands:
            broken = True
            log.append("O(i+1)=%d %d obj not found -> broken file" % (n2, g2))
            break
        if len(cands) > 1:
            log.append("O(i+1)=%d: %d candidates pass eq 1.5, first in image order taken; others at %s"
                       % (n2, len(cands), cands[1:6]))
        c = cands[0]
        cur = _Frag(objs=[(n2, g2, o2, c)], delta=c - (h + o2))
        i += 1
    if not broken:
        frags.append(cur)
    # Temporary boundaries in absolute image offsets (eqs 1.9-1.10).
    for k, f in enumerate(frags):
        f.head = _frag_head(f.objs[0][3])
        f.foot = _frag_foot(f.objs[-1][3])
    if frags:
        frags[0].head = h                                   # the file starts at its header
        if not broken:
            last = frags[-1]
            e = _eof_after(image, last.objs[-1][3], len(image))   # IV.A footer
            if e > 0:
                last.foot = e
    return frags, fobjN, broken, entries


def _candidate_cuts(fa: _Frag, fb: _Frag, h: int) -> Tuple[List[int], int]:
    """Logical cut positions p (file offsets) between the two temporary
    boundaries, latest first, and streamSN (eq 1.13, see module docstring).
    A cut is valid only where both fragments break on a sector boundary."""
    p_max = fb.head - h - fb.delta          # head of fragment j+1, as a file offset
    p_min = fa.foot - h - fa.delta          # foot of fragment j, as a file offset
    if (fb.delta - fa.delta) % SECTOR_SIZE or p_max < p_min:
        return [], -1
    streamSN = (p_max - p_min) // SECTOR_SIZE                   # eq 1.13
    return [p_max - c * SECTOR_SIZE for c in range(streamSN + 1)], streamSN


def _edv(image, fa: _Frag, fb: _Frag, h: int) -> Tuple[Optional[int], str]:
    """Section IV.C (eq 1.12). Confirmed only if exactly one candidate sector
    boundary between the temporary boundaries reaches HD >= 0.8."""
    cuts, _ = _candidate_cuts(fa, fb, h)
    hits = [p for p in cuts if _hd(image, h + fa.delta + p) >= ENTROPY_TH]
    if len(hits) == 1:
        return hits[0], "EDV confirmed (HD>=%.1f at one of %d boundaries)" % (ENTROPY_TH, len(cuts))
    return None, "EDV unconfirmed (%d of %d boundaries reach HD>=%.1f)" % (len(hits), len(cuts), ENTROPY_TH)


def _zdv(image, fa: _Frag, fb: _Frag, h: int, entries, deltas) -> Tuple[Optional[int], str]:
    """Section IV.D, steps 1-9. stream1 = Flate data of the fragmented object
    O(i) in fragment j; stream2 = the rest of its stream in fragment j+1. Join
    and decompress; on failure move one sector from the end of stream1 to the
    head of stream2 and repeat until c surpasses streamSN (eq 1.13)."""
    num, gen, off_i, toff_i = fa.objs[-1]
    oi = _OBJ_RE.match(image, toff_i)
    if not oi:
        return None, "ZDV n/a: O(i) header not found"
    sm = _STREAM_RE.search(image, oi.end(), oi.end() + 65536)
    eo = image.find(b"endobj", oi.end(), oi.end() + 65536)
    if not sm or (0 <= eo < sm.start()):
        return None, "ZDV n/a: O(i) is not a stream"
    dict_bytes = bytes(image[oi.end():sm.start()])
    if not _FLATE_RE.search(dict_bytes):
        return None, "ZDV n/a: O(i) is not FlateDecode"
    L = _resolve_length(image, dict_bytes, entries, deltas, h)
    if L is None:
        return None, "ZDV n/a: /Length unresolved"
    s_log = sm.end() - h - fa.delta                 # stream data start, as a file offset
    e_log = s_log + L                               # stream data end, as a file offset
    cuts, streamSN = _candidate_cuts(fa, fb, h)
    if streamSN < 0:
        return None, "ZDV n/a: fragments not sector-aligned relative to each other"
    for c, p in enumerate(cuts):                    # steps 3, 5, 6, 7
        if not (s_log < p < e_log):
            continue
        stream1 = bytes(image[h + fa.delta + s_log: h + fa.delta + p])     # step 1
        stream2 = bytes(image[h + fb.delta + p: h + fb.delta + e_log])     # step 2
        if _inflates(stream1 + stream2):                                   # step 5
            return p, "ZDV confirmed at c=%d of streamSN=%d (step 9)" % (c, streamSN)
    return None, "ZDV unsuccessful after streamSN=%d (step 8)" % streamSN


def run_cascade(image, h: int, limit: int) -> Dict[str, object]:
    """Figure 2 for the PDF whose header is at absolute offset h; limit bounds
    the forward scans of IV.A."""
    log: List[str] = []
    intact, hfv_note, secs, linearized = _hfv(image, h, limit)
    if intact is not None:
        return dict(path="HFV", pdf=intact, hfv=hfv_note, isv="", edv="", zdv="", fragments=[(h, h + len(intact))], log=log)
    frags, fobjN, broken, entries = _isv(image, h, limit, secs, log)
    if not frags:
        return dict(path="FAIL", pdf=None, hfv=hfv_note, isv="ISV: no fragments (" + "; ".join(log) + ")",
                    edv="", zdv="", fragments=[], log=log)
    fragN = fobjN + 1                                                    # eq 1.8
    isv_note = "ISV fragN=%d%s" % (fragN, " broken" if broken else "")
    deltas = [f.delta for f in frags]
    edv_notes, zdv_notes, how = [], [], []
    for j in range(len(frags) - 1):
        fa, fb = frags[j], frags[j + 1]
        p, en = _edv(image, fa, fb, h)                                   # Figure 2: C before D
        edv_notes.append(en)
        if p is None:
            p, zn = _zdv(image, fa, fb, h, entries, deltas)
            zdv_notes.append(zn)
            how.append("ZDV" if p is not None else "temporary")
        else:
            how.append("EDV")
        if p is not None:                                                # step 9: confirmed boundaries
            fa.foot = h + fa.delta + p
            fb.head = h + fb.delta + p
    pdf = b"".join(bytes(image[f.head:f.foot]) for f in frags if f.foot > f.head)
    path = "ISV" if not how else "+".join(how)
    return dict(path=path, pdf=pdf, hfv=hfv_note, isv=isv_note, edv="; ".join(edv_notes),
                zdv="; ".join(zdv_notes), fragments=[(f.head, f.foot) for f in frags], log=log)


def run_all_algorithms(raw_data: bytes, image=None, start: int = 0) -> dict:
    """Run the cascade on one candidate region and report it per algorithm.

    raw_data -- the region from a %PDF- header (as cut by chen_carve.py)
    image    -- the whole disk image (bytes or mmap); defaults to raw_data
    start    -- absolute offset of raw_data inside image
    """
    t0 = time.perf_counter()
    if image is None:
        image, start = raw_data, 0
    hdr = _HEADER_RE.search(raw_data)
    if not hdr:
        r = ChenResult("A5_PC", None, False, None, 0, time.perf_counter() - t0, "no %PDF- header")
        return {k: r for k in ("A1_HFV", "A2_ISV", "A3_EDV", "A4_ZDV", "A5_PC")}
    h = start + hdr.start()
    res = run_cascade(image, h, start + len(raw_data))
    pdf = res["pdf"]
    ok, pages = _try_pikepdf(pdf) if pdf else (False, None)
    el = time.perf_counter() - t0
    nob = _count_objects(pdf) if pdf else 0
    detail = "cascade=%s; %s; %s; %s; %s; fragments=%s%s" % (
        res["path"], res["hfv"], res["isv"], res["edv"] or "EDV=n/a", res["zdv"] or "ZDV=n/a",
        res["fragments"], ("; log: " + " | ".join(res["log"])) if res["log"] else "")
    pc = ChenResult("A5_PC", pdf if ok else None, ok, pages, nob, el, detail)
    return {
        "A1_HFV": ChenResult("A1_HFV", pdf if res["path"] == "HFV" and ok else None,
                             res["path"] == "HFV" and ok, pages if res["path"] == "HFV" else None, nob, el, res["hfv"]),
        "A2_ISV": ChenResult("A2_ISV", None, False, None, 0, el, res["isv"] or "n/a (intact)"),
        "A3_EDV": ChenResult("A3_EDV", None, False, None, 0, el, res["edv"] or "n/a"),
        "A4_ZDV": ChenResult("A4_ZDV", None, False, None, 0, el, res["zdv"] or "n/a"),
        "A5_PC": pc,
    }
