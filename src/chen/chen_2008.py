"""
chen_2008.py — Faithful implementation of the Chen et al. (2008) PDF carving
validation cascade.

Paper
-----
Chen, M.; Zheng, N.; Xu, M.; Lou, Y.; Wang, X. (2008).
"Validation Algorithms Based on Content Characters and Internal Structure:
The PDF File Carving Method." 2008 International Symposium on Information
Science and Engineering (ISISE), pp. 168-172. DOI 10.1109/ISISE.2008.209.

The paper defines FIVE *validation* algorithms (Section IV.A-E) executed as
the cascade in Figure 2. They progressively classify disk-image bytes as
intact / fragmented / broken PDF and resolve sector-aligned fragment
boundaries.

Algorithm map (paper -> code identifier)
----------------------------------------
  A. Header/file-length/maximal-offset/footer validation   -> A1_HFV
       Linearised:     f = h + l                            (eq 1.1)
       Non-linearised: objM = h + offM                      (eq 1.2)

  B. Internal-structure validation                          -> A2_ISV
       Intact-object test:  Len1 = Off2 - Off1              (eq 1.3)
                            Len2 = Toff2 - Toff1            (eq 1.4)
                            intact iff Len1 == Len2          (eq 1.5)
       Multi-section xref merge: y1 = x2, S = y1 + y2       (eqs 1.6-1.7)
       Fragment count:      fragN = fobjN + 1               (eq 1.8)
       Temporary bounds:    fragH = floor(objF / 512) * 512 (eq 1.9)
                            fragF = (floor(objL/512)+1)*512 (eq 1.10)

  C. Entropy-difference validation                          -> A3_EDV
       Shannon:  H(x) = -sum p(i) log2 p(i)                 (eq 1.11)
       HD(x)  =  | H(window_before) - H(window_after) |     (eq 1.12)
       Boundary confirmed when HD >= 0.8 (paper, p. 171).
       Window size n = m = 256 bytes (eq 1.12 caption).

  D. Zlib/deflate decompression validation                  -> A4_ZDV
       streamSN = floor((objLen - streamL1 - streamL2)/512) (eq 1.13)
       Confirms cross-fragment FlateDecode streams by reassembling
       stream1 + (gap of streamSN sectors) + stream2 and decoding.

  E. Character-table validation                              folded into A5_PC
       Detects font subset tags `/BaseFont /XXXXXX+Name` (6 uppercase
       letters + '+' + Adobe standard font name; paper §IV.E) that
       straddle a 512-byte sector boundary, confirming the two sectors
       are adjacent in the same PDF.

  A5_PC: full Figure-2 cascade -- A -> B -> C -> (D ∧ E) -> output bytes.

Sector size 512 B is hard-coded throughout the paper (FAT32 context,
eqs 1.9 / 1.10 / 1.13).

API
---
``run_all_algorithms(raw_data) -> dict[str, ChenResult]`` with keys
``A1_HFV, A2_ISV, A3_EDV, A4_ZDV, A5_PC``.  ChenResult shape is
preserved from the previous module so existing benchmark callers
continue to work; ``results["A5_PC"]`` is the cascade output that
downstream scripts consume.
"""

from __future__ import annotations

import io
import logging
import math
import re
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Paper-fixed constants
# ------------------------------------------------------------------

SECTOR_SIZE  = 512   # FAT32 sector, eqs 1.9 / 1.10 / 1.13
ENTROPY_WIN  = 256   # n = m = 256, eq 1.12 caption
ENTROPY_TH   = 0.8   # paper §IV.C: "good result when HD value equals to 0.8"
DICT_SCAN_MAX = 8192 # cap dictionary scan per object

# ------------------------------------------------------------------
# Regex toolbox
# ------------------------------------------------------------------

_HEADER_RE  = re.compile(rb"%PDF-\d+\.\d+")
_OBJ_RE     = re.compile(rb"(?<!\d)(\d{1,7})\s+(\d{1,5})\s+obj\b")
_SXREF_RE   = re.compile(rb"\bstartxref\s+(\d+)")
_EOF_RE     = re.compile(rb"%%EOF")
_LEN_RE     = re.compile(rb"/Length\s+(\d{1,12})")
_LIN_L_RE   = re.compile(rb"/Linearized\b[^>]{0,512}?/L\s+(\d{1,12})", re.DOTALL)
_PREV_RE    = re.compile(rb"/Prev\s+(\d{1,12})")
_FLATE_RE   = re.compile(rb"/Filter\s*(?:/FlateDecode\b|\[\s*[^\]]*?/FlateDecode[^\]]*?\])")
_STREAM_RE  = re.compile(rb"\bstream[ \t]*\r?\n")
# Font subset tag: 6 uppercase + '+' + font name (paper §IV.E)
_FONT_TAG_RE = re.compile(
    rb"/(?:BaseFont|FontName)\s*/([A-Z]{6})\+([A-Za-z0-9_\-]+)"
)
# Adobe 14 standard fonts (paper cites "Adobe font name reference table")
_STD_FONTS = frozenset({
    b"Helvetica", b"Helvetica-Bold", b"Helvetica-Oblique", b"Helvetica-BoldOblique",
    b"Times-Roman", b"Times-Bold", b"Times-Italic", b"Times-BoldItalic",
    b"Courier", b"Courier-Bold", b"Courier-Oblique", b"Courier-BoldOblique",
    b"Symbol", b"ZapfDingbats",
})


# ------------------------------------------------------------------
# Public dataclass (shape preserved from previous module)
# ------------------------------------------------------------------

@dataclass
class ChenResult:
    alg_used:   Optional[str]
    pdf_bytes:  Optional[bytes]
    renderable: bool
    page_count: Optional[int]
    n_objects:  int
    elapsed_s:  float
    notes:      str = ""


# ==================================================================
# Helpers
# ==================================================================

def _try_pikepdf(pdf_bytes: bytes) -> Tuple[bool, Optional[int]]:
    """Return (renderable, page_count) using pikepdf."""
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


def _sector_floor(pos: int) -> int:
    """fragH = floor(pos / 512) * 512    (eq 1.9)"""
    return (pos // SECTOR_SIZE) * SECTOR_SIZE


def _sector_ceil(pos: int) -> int:
    """fragF = (floor(pos / 512) + 1) * 512    (eq 1.10)"""
    return ((pos // SECTOR_SIZE) + 1) * SECTOR_SIZE


def _shannon_entropy(buf: bytes) -> float:
    """H(x) = -sum p(i) log2 p(i)    (eq 1.11)"""
    if not buf:
        return 0.0
    n = len(buf)
    h = 0.0
    for c in Counter(buf).values():
        p = c / n
        h -= p * math.log2(p)
    return h


def _parse_xref_table(
    data: bytes, xref_pos: int
) -> Optional[List[Tuple[int, int, int, bool]]]:
    """
    Parse a classical ASCII xref table starting at or near `xref_pos`.

    Returns list of (object_number, offset, generation, in_use), or None
    on failure. Handles multiple subsections.

    Per PDF Ref §3.4.3: each entry is exactly 20 bytes
        nnnnnnnnnn ggggg n EOL   (n = in-use, f = free).
    """
    n = len(data)
    if xref_pos < 0 or xref_pos >= n:
        return None
    # Snap to nearest "xref" keyword within +/- 64 B
    if data[xref_pos:xref_pos + 4] != b"xref":
        idx = data.find(b"xref", max(0, xref_pos - 64), xref_pos + 64)
        if idx < 0:
            return None
        xref_pos = idx
    pos = xref_pos + 4
    while pos < n and data[pos:pos + 1] in (b" ", b"\r", b"\n", b"\t"):
        pos += 1

    entries: List[Tuple[int, int, int, bool]] = []
    while pos < n:
        # Subsection header "first count"
        eol = data.find(b"\n", pos, pos + 64)
        if eol < 0:
            break
        line = data[pos:eol].strip().rstrip(b"\r")
        if not line or line.startswith(b"trailer"):
            break
        parts = line.split()
        if len(parts) != 2:
            break
        try:
            first = int(parts[0]); count = int(parts[1])
        except ValueError:
            break
        pos = eol + 1
        if count <= 0 or count > 10_000_000:
            break
        for i in range(count):
            if pos + 20 > n:
                return None
            ent = data[pos:pos + 20]
            try:
                offset = int(ent[0:10])
                gen    = int(ent[11:16])
                flag   = ent[17:18]
            except ValueError:
                return None
            entries.append((first + i, offset, gen, flag == b"n"))
            pos += 20
    return entries if entries else None


def _collect_xref_chain(data: bytes, h: int) -> List[Tuple[int, int, int, bool]]:
    """
    Pretreatment per paper §IV.B.2.a (eqs 1.6-1.7): merge cross-reference
    sections chained through the trailer's /Prev pointer.

    Later entries override earlier ones for the same object number, which
    is the standard PDF interpretation of incremental updates.
    """
    merged: dict = {}
    visited: set = set()

    sx = None
    for m in _SXREF_RE.finditer(data):
        sx = m   # take the LAST startxref (newest update)
    if not sx:
        return []
    next_offset: Optional[int] = int(sx.group(1))

    while next_offset is not None and next_offset not in visited:
        visited.add(next_offset)
        abs_pos = h + next_offset if (h + next_offset) < len(data) else next_offset
        entries = _parse_xref_table(data, abs_pos)
        if not entries:
            break
        # Locate trailer dict after this xref block to read /Prev
        end_of_xref = abs_pos
        tr_idx = data.find(b"trailer", end_of_xref, end_of_xref + 64 + 25 * len(entries))
        next_offset = None
        if tr_idx >= 0:
            tail = data[tr_idx:tr_idx + 4096]
            pm = _PREV_RE.search(tail)
            if pm:
                next_offset = int(pm.group(1))
        # Older sections only fill objects not seen yet (incremental-update rule)
        for num, off, gen, in_use in entries:
            merged.setdefault(num, (num, off, gen, in_use))

    return sorted(merged.values(), key=lambda e: e[0])


def _find_obj_at(data: bytes, num: int, search_from: int, search_to: int) -> int:
    """
    Return absolute offset of `N G obj` in data within [from, to), or -1.
    """
    pat = re.compile(rb"\b%d\s+\d{1,5}\s+obj\b" % num)
    m = pat.search(data, max(0, search_from), min(len(data), search_to))
    return m.start() if m else -1


def _last_eof_after(data: bytes, pos: int) -> int:
    """rfind %%EOF after `pos`, else -1."""
    idx = data.rfind(b"%%EOF")
    return idx if idx > pos else -1


# ==================================================================
# A1 -- Header/length/maximal-offset/footer validation  (paper §IV.A)
# ==================================================================

def algorithm_hfv(data: bytes) -> ChenResult:
    """
    Classify the region as intact / default-fragmented per paper §IV.A.

    Linearised PDFs:
        Read /L from the linearization parameter dictionary, check
        f = h + l   (eq 1.1)   --> %%EOF must sit at f.

    Non-linearised PDFs:
        Parse the cross-reference table, take the maximal in-use offset
        offM, and verify   objM = h + offM   (eq 1.2)   -- i.e. an
        `N G obj` keyword is present exactly at that location.
    """
    t0 = time.perf_counter()
    hdr = _HEADER_RE.search(data)
    if not hdr:
        return ChenResult("A1_HFV", None, False, None, 0,
                          time.perf_counter() - t0, "no %PDF- header")
    h = hdr.start()

    # ---- Linearised path (eq 1.1) ----------------------------------
    lin = _LIN_L_RE.search(data, h, h + 4096)
    if lin:
        L = int(lin.group(1))
        f = h + L                                            # eq 1.1
        if f <= len(data):
            tail = data[max(h, f - 32): min(len(data), f + 16)]
            if b"%%EOF" in tail:
                pdf_bytes = data[h:f]
                ok, pages = _try_pikepdf(pdf_bytes)
                return ChenResult(
                    "A1_HFV", pdf_bytes if ok else None, ok, pages,
                    _count_objects(pdf_bytes), time.perf_counter() - t0,
                    "linearised intact (eq 1.1)" if ok
                    else "linearised length matched but pikepdf failed",
                )
        return ChenResult("A1_HFV", None, False, None,
                          _count_objects(data), time.perf_counter() - t0,
                          "linearised /L mismatch -> default-fragmented")

    # ---- Non-linearised path (eq 1.2) ------------------------------
    entries = _collect_xref_chain(data, h)
    if not entries:
        return ChenResult("A1_HFV", None, False, None, 0,
                          time.perf_counter() - t0,
                          "no parseable xref -> default-fragmented")
    in_use = [(num, off, gen) for (num, off, gen, used) in entries
              if used and off > 0]
    if not in_use:
        return ChenResult("A1_HFV", None, False, None, 0,
                          time.perf_counter() - t0,
                          "xref empty of in-use entries -> default-fragmented")

    offM = max(off for (_, off, _) in in_use)
    objM_pos = h + offM                                       # eq 1.2
    if objM_pos >= len(data):
        return ChenResult("A1_HFV", None, False, None, 0,
                          time.perf_counter() - t0,
                          f"objM={objM_pos} > image -> default-fragmented")
    # Probe for `N G obj` at objM (allow small slack to absorb whitespace)
    probe = data[objM_pos: objM_pos + 64]
    if not re.match(rb"\d{1,7}\s+\d{1,5}\s+obj\b", probe):
        return ChenResult("A1_HFV", None, False, None, 0,
                          time.perf_counter() - t0,
                          "objM probe mismatch -> default-fragmented")

    eof = _last_eof_after(data, objM_pos)
    if eof < 0:
        return ChenResult("A1_HFV", None, False, None, 0,
                          time.perf_counter() - t0,
                          "no %%EOF after objM -> default-fragmented")
    pdf_bytes = data[h: eof + 5]
    ok, pages = _try_pikepdf(pdf_bytes)
    return ChenResult(
        "A1_HFV", pdf_bytes if ok else None, ok, pages,
        _count_objects(pdf_bytes), time.perf_counter() - t0,
        "non-linearised intact (eq 1.2)" if ok
        else "objM matched but pikepdf failed",
    )


# ==================================================================
# A2 -- Internal-structure validation  (paper §IV.B)
# ==================================================================

def _isv_fragment_plan(data: bytes) -> Tuple[List[Tuple[int, int]], List[int], int]:
    """
    Apply intact-object validation (eqs 1.3-1.5) to a default-fragmented
    region; compute fragN (eq 1.8) and per-fragment temporary bounds
    (eqs 1.9-1.10).

    Returns (fragments, internal_boundaries, fragN) where:
        fragments  : [(fragH, fragF), ...] absolute byte ranges
        internal_boundaries : list of absolute positions between fragments
                              (suitable input to EDV)
        fragN      : number of fragments
    """
    hdr = _HEADER_RE.search(data)
    if not hdr:
        return [], [], 0
    h = hdr.start()

    entries = _collect_xref_chain(data, h)
    in_use = sorted(
        [(num, off, gen) for (num, off, gen, used) in entries
         if used and off > 0],
        key=lambda e: e[1],
    )
    if len(in_use) < 2:
        return [(h, len(data))], [], 1

    # Eqs 1.3-1.5: walk consecutive xref-ordered objects
    intact_flags: List[bool] = []
    toff_cache: dict = {}
    for num, off, _ in in_use:
        toff_cache[num] = _find_obj_at(
            data, num, h + off - 16, h + off + 4096
        )
    for i in range(len(in_use) - 1):
        num1, off1, _ = in_use[i]
        num2, off2, _ = in_use[i + 1]
        Toff1 = toff_cache[num1]
        Toff2 = toff_cache[num2]
        if Toff1 < 0 or Toff2 < 0:
            intact_flags.append(False)
            continue
        Len1 = off2 - off1              # eq 1.3
        Len2 = (Toff2 - h) - (Toff1 - h)  # eq 1.4
        intact_flags.append(Len1 == Len2)  # eq 1.5

    # Eq 1.8: fragN = fobjN + 1
    fobjN = sum(1 for f in intact_flags if not f)
    fragN = fobjN + 1

    # Eqs 1.9-1.10: temporary boundaries per fragment
    splits = [0]
    for i, intact in enumerate(intact_flags):
        if not intact:
            splits.append(i + 1)
    splits.append(len(in_use))

    fragments: List[Tuple[int, int]] = []
    internal: List[int] = []
    for fi in range(len(splits) - 1):
        group = in_use[splits[fi]:splits[fi + 1]]
        if not group:
            continue
        objF = group[0][1]
        objL = group[-1][1]
        fragH = _sector_floor(h + objF)       # eq 1.9
        fragF = _sector_ceil(h + objL)        # eq 1.10
        if fi == 0:
            fragH = h                         # preserve %PDF- header
        if fi == len(splits) - 2:
            eof = _last_eof_after(data, h + objL)
            if eof > 0:
                fragF = min(_sector_ceil(eof + 5), len(data))
        fragments.append((fragH, fragF))
        if fi > 0:
            internal.append(fragH)
    return fragments, internal, fragN


def algorithm_isv(data: bytes) -> ChenResult:
    """
    Internal-structure validation cascade per paper §IV.B.

    Computes intact-object flags (eqs 1.3-1.5), the fragment count
    (eq 1.8), and temporary fragment bounds (eqs 1.9-1.10), then
    assembles the candidate PDF by concatenating fragment byte ranges
    in xref order.
    """
    t0 = time.perf_counter()
    fragments, _, fragN = _isv_fragment_plan(data)
    if not fragments:
        return ChenResult("A2_ISV", None, False, None, 0,
                          time.perf_counter() - t0, "isv: no fragments planned")
    if fragN == 1:
        fH, fF = fragments[0]
        pdf_bytes = data[fH:fF]
        ok, pages = _try_pikepdf(pdf_bytes)
        return ChenResult("A2_ISV", pdf_bytes if ok else None, ok, pages,
                          _count_objects(pdf_bytes),
                          time.perf_counter() - t0,
                          "isv: fragN=1 (no internal break)")
    pdf_bytes = b"".join(data[fH:fF] for fH, fF in fragments)
    ok, pages = _try_pikepdf(pdf_bytes)
    return ChenResult(
        "A2_ISV", pdf_bytes if ok else None, ok, pages,
        _count_objects(pdf_bytes), time.perf_counter() - t0,
        f"isv: fragN={fragN} fragments={fragments}",
    )


# ==================================================================
# A3 -- Entropy-difference validation  (paper §IV.C)
# ==================================================================

def _hd_at(data: bytes, b: int) -> float:
    """HD(x) at boundary `b` (eq 1.12)."""
    lo = b - ENTROPY_WIN
    hi = b + ENTROPY_WIN
    if lo < 0 or hi > len(data):
        return 0.0
    h1 = _shannon_entropy(data[lo:b])
    h2 = _shannon_entropy(data[b:hi])
    return abs(h1 - h2)


def algorithm_edv(data: bytes) -> ChenResult:
    """
    Entropy-difference validation per paper §IV.C.

    For each ISV-proposed boundary, compute HD over the 256 bytes either
    side (eq 1.12). A boundary is *confirmed* iff HD >= 0.8.

    EDV is purely a validator -- it does not carve. The result's
    pdf_bytes are passed through from ISV; the notes report the
    confirmation verdict.
    """
    t0 = time.perf_counter()
    fragments, internal, fragN = _isv_fragment_plan(data)
    isv_bytes: Optional[bytes] = None
    isv_ok = False
    isv_pages = None
    isv_nobj = 0
    if fragments:
        if fragN == 1:
            fH, fF = fragments[0]
            isv_bytes = data[fH:fF]
        else:
            isv_bytes = b"".join(data[fH:fF] for fH, fF in fragments)
        isv_ok, isv_pages = _try_pikepdf(isv_bytes)
        isv_nobj = _count_objects(isv_bytes)

    if not internal:
        return ChenResult("A3_EDV", isv_bytes if isv_ok else None,
                          isv_ok, isv_pages, isv_nobj,
                          time.perf_counter() - t0,
                          "edv: no internal boundaries to confirm")

    hds = [(b, _hd_at(data, b)) for b in internal]
    confirmed = sum(1 for _, hd in hds if hd >= ENTROPY_TH)
    detail = ", ".join(f"@{b}:HD={hd:.2f}" for b, hd in hds)
    return ChenResult(
        "A3_EDV", isv_bytes if isv_ok else None, isv_ok, isv_pages, isv_nobj,
        time.perf_counter() - t0,
        f"edv: {confirmed}/{len(hds)} boundaries confirmed (HD>={ENTROPY_TH}); {detail}",
    )


# ==================================================================
# A4 -- Zlib/deflate decompression validation  (paper §IV.D)
# ==================================================================

def _stream_decodes(raw: bytes) -> bool:
    """True iff `raw` is a valid zlib/deflate stream end-to-end."""
    if not raw:
        return False
    for wbits in (15, -15):
        try:
            zlib.decompress(raw, wbits)
            return True
        except zlib.error:
            continue
    return False


def algorithm_zdv(data: bytes) -> ChenResult:
    """
    Zlib/deflate decompression validation per paper §IV.D.

    Walks every FlateDecode stream object; first attempts to decode the
    declared /Length range as-is. On failure (stream split across a
    fragment), applies eq 1.13:

            streamSN = floor((objLen - streamL1 - streamL2) / 512)

    by trying to skip 1..16 sector-sized gaps inside the stream and
    re-decode; success confirms the cross-fragment join.

    Purely a validator; pdf_bytes pass through from ISV.
    """
    t0 = time.perf_counter()
    n_streams = 0
    n_intact  = 0
    n_repaired = 0
    n_failed = 0

    for m in _OBJ_RE.finditer(data):
        obj_start = m.start()
        nxt = _OBJ_RE.search(data, obj_start + 1)
        end_scan = min(nxt.start() if nxt else len(data),
                       obj_start + DICT_SCAN_MAX)
        chunk = data[obj_start:end_scan]
        if not _FLATE_RE.search(chunk):
            continue
        len_m = _LEN_RE.search(chunk)
        if not len_m:
            continue
        objLen = int(len_m.group(1))
        sm = _STREAM_RE.search(chunk)
        if not sm:
            continue
        s_start = obj_start + sm.end()
        s_end   = s_start + objLen
        if s_end > len(data):
            continue
        n_streams += 1
        raw = data[s_start:s_end]
        if _stream_decodes(raw):
            n_intact += 1
            continue
        # Apply eq 1.13: try skipping 1..16 sectors of intrusion
        recovered = False
        for sn in range(1, 17):
            for cut in range(SECTOR_SIZE, min(len(raw), 4096), SECTOR_SIZE):
                shifted = raw[:cut] + raw[cut + sn * SECTOR_SIZE:]
                if _stream_decodes(shifted):
                    recovered = True
                    break
            if recovered:
                break
        if recovered:
            n_repaired += 1
        else:
            n_failed += 1

    isv = algorithm_isv(data)
    return ChenResult(
        "A4_ZDV", isv.pdf_bytes, isv.renderable, isv.page_count, isv.n_objects,
        time.perf_counter() - t0,
        f"zdv: streams={n_streams} intact={n_intact} "
        f"repaired={n_repaired} failed={n_failed} (eq 1.13)",
    )


# ==================================================================
# Algorithm E -- Character-table validation  (paper §IV.E)
# ==================================================================

def _ctv_scan(data: bytes) -> Tuple[int, int, int]:
    """
    Returns (n_subset_tags, n_standard, n_cross_sector).

    Subset tag pattern: /BaseFont /XXXXXX+Name   (paper §IV.E)
    Cross-sector: tag start falls in the last 32 bytes of a 512-byte sector
    -> its 6-uppercase prefix and '+name' tail straddle the boundary,
    confirming the two sectors are adjacent in the same PDF.
    """
    subset = 0
    std    = 0
    cross  = 0
    for m in _FONT_TAG_RE.finditer(data):
        subset += 1
        name = m.group(2)
        if name in _STD_FONTS:
            std += 1
        pos_in_sector = m.start() % SECTOR_SIZE
        if pos_in_sector > SECTOR_SIZE - 32:
            cross += 1
    return subset, std, cross


# ==================================================================
# A5 -- Progressive cascade  (paper Figure 2)
# ==================================================================

def algorithm_pc(data: bytes) -> ChenResult:
    """
    Full Figure-2 pipeline:

       disk image
            |
            v
        [A: HFV] -> intact ?  --> emit bytes h..%%EOF (eq 1.1 or 1.2)
            | default-fragmented
            v
        [B: ISV] -> fragment plan via eqs 1.3-1.10
            |
            v
        [C: EDV] -> confirm boundaries with HD >= 0.8 (eq 1.12)
            |
            v
        [D: ZDV]  cross-fragment stream check (eq 1.13)
        [E: CTV]  font-tag cross-sector check (§IV.E)
            |
            v
        emit assembled PDF bytes

    Returns the cascade's final bytes (intact PDF if A succeeds,
    otherwise ISV-assembled bytes annotated with C/D/E verdicts).
    """
    t0 = time.perf_counter()

    # --- Step A ---
    a = algorithm_hfv(data)
    if a.renderable and a.pdf_bytes:
        return ChenResult(
            "A5_PC", a.pdf_bytes, True, a.page_count, a.n_objects,
            time.perf_counter() - t0,
            f"cascade=HFV ({a.notes})",
        )

    # --- Step B ---
    fragments, internal, fragN = _isv_fragment_plan(data)
    if not fragments:
        return ChenResult(
            "A5_PC", None, False, None, 0,
            time.perf_counter() - t0,
            "cascade failed: HFV no-intact, ISV no-fragments",
        )
    pdf_bytes = b"".join(data[fH:fF] for fH, fF in fragments)

    # --- Step C ---
    if internal:
        hds = [_hd_at(data, b) for b in internal]
        edv_confirmed = sum(1 for hd in hds if hd >= ENTROPY_TH)
        edv_note = f"EDV={edv_confirmed}/{len(internal)}>=0.8"
    else:
        edv_note = "EDV=n/a"

    # --- Step D ---
    zdv = algorithm_zdv(data)
    # --- Step E ---
    n_sub, n_std, n_cross = _ctv_scan(data)
    ctv_note = f"CTV=subset:{n_sub} std:{n_std} crossSector:{n_cross}"

    ok, pages = _try_pikepdf(pdf_bytes)
    return ChenResult(
        "A5_PC", pdf_bytes if ok else None, ok, pages,
        _count_objects(pdf_bytes), time.perf_counter() - t0,
        f"cascade=ISV fragN={fragN}; {edv_note}; "
        f"{zdv.notes.split(': ', 1)[-1]}; {ctv_note}",
    )


# ==================================================================
# Public entry point
# ==================================================================

def run_all_algorithms(raw_data: bytes) -> dict:
    """
    Run the five paper algorithms on `raw_data`.

    Returns a dict with keys:
        A1_HFV  -- header/length/maximal-offset/footer validation
        A2_ISV  -- internal-structure validation
        A3_EDV  -- entropy-difference validation
        A4_ZDV  -- zlib/deflate cross-fragment validation
        A5_PC   -- full Figure-2 cascade (A -> B -> C -> D/E -> bytes)

    The shape of each value (ChenResult) is preserved from earlier
    versions so existing benchmark callers continue to work; the
    cascade result is at key "A5_PC".
    """
    return {
        "A1_HFV": algorithm_hfv(raw_data),
        "A2_ISV": algorithm_isv(raw_data),
        "A3_EDV": algorithm_edv(raw_data),
        "A4_ZDV": algorithm_zdv(raw_data),
        "A5_PC":  algorithm_pc(raw_data),
    }
