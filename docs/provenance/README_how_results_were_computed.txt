===============================================================================
 EVALUATION METHODOLOGY — SCRIPT EVIDENCE
 How every number in the result workbook was computed.
===============================================================================

This folder preserves, as plain-text copies, every script used to score the
carving experiments. They are kept verbatim so a reviewer can audit exactly
how each figure in the result tables was produced. Nothing here is a summary
or a paraphrase — each .txt is a byte-for-byte copy of the actual script run.

The live/runnable copies live at their source paths (listed per file below);
these .txt copies are the frozen evidence versions.

-------------------------------------------------------------------------------
 THE SCORING PIPELINE (produces the result tables)
-------------------------------------------------------------------------------

SCORING_render_hash_classifier_all_6_tools.py.txt
    Source: <repo>\eval\score_corpus.py
    The main scorer. Compares every carved PDF from all six tools (scar,
    cohen, chen, foremost, scalpel, photorec) against ground truth using a
    three-tier hash cascade:
        Tier 1  File hash    SHA-256 of the raw carved PDF bytes.
                             Byte-identical match -> FAITHFUL.
        Tier 2  Render-hash  MD5 of each page's rendered pixel buffer
                             (page.get_pixmap(dpi=72).samples), kept as an
                             ordered per-page tuple. This is the PRIMARY
                             check: content-faithful match even when the file
                             bytes changed (carvers re-serialize the PDF, so
                             file SHA rarely survives a correct recovery, but
                             the rendered pixels do).
        Tier 3  Text-hash    SHA-256 of concatenated extracted page text
                             (page.get_text("text"), joined with form-feed).
                             Secondary signal for PARTIAL credit.
    Classification:
        FAITHFUL   file SHA-256 matches, OR full per-page render-hash tuple matches.
        PARTIAL    carved pages are an ordered subset of a ground-truth doc's
                   pages, OR text-hash matches but render doesn't.
        DUPLICATE  matches the SAME ground-truth doc as an already-counted,
                   better-or-equal candidate. Kept SEPARATE from FALSE_ACC on
                   purpose: a redundant-but-correct reconstruction (e.g. Cohen's
                   un-deduplicated candidates from ambiguous xref matches) is a
                   different, far less severe outcome than a fabricated/wrong
                   one. Counting duplicates as false accepts would unfairly
                   punish tools that simply don't run a dedup pass.
        FALSE_ACC  renders fine but matches no ground-truth document at all.
        JUNK       does not render at all.
    Metrics:
        Recall_%  = Faithful / GT_docs * 100
        FA_rate_% = False_accept / (Faithful + Partial + False_accept) * 100
    DFRWS exception: the 2007 challenge never released its source PDFs, only a
    published MD5 answer key, so that ONE corpus is scored by exact whole-file
    MD5 against the answer key (no render tier possible).

    EXACT COMMAND USED (parallel, 20 worker processes, one per image):
        py score_corpus.py ^
           --results       <repo>\results\full_carved_output ^
           --dataset       <repo>\dataset ^
           --ground-truth  <repo>\ground_truth ^
           --workers 20 ^
           --out <repo>\evaluation\scored_results_render_hash.csv

    Verified: the parallel run produces byte-identical output to a single-worker
    (sequential) run — confirmed by diff on a test image before the full sweep.

SCORING_byte_level_fidelity_T1_T2_tiers.py.txt
    Source: <repo>\eval\groundtruth.py
    A complementary byte-level fidelity check (T1 exact SHA-256, T2 common
    prefix/suffix byte-diff that classifies a mismatch as trailer/padding-only
    vs. real content divergence). Used to characterise HOW a near-miss differs
    from ground truth, for the "minor byte error vs. content error" distinction.
    Deliberately uses a fast O(n) chunked prefix/suffix comparison, NOT difflib
    (difflib is pathologically slow on binary/compressed PDF data and models
    scattered edits, which is the wrong model for carving truncation points).

-------------------------------------------------------------------------------
 SCAR-INTERNAL VALIDATORS (referenced by the pipeline; run inside carve.py)
-------------------------------------------------------------------------------

SCAR_internal_P1_to_P6_validator.py.txt
    Source: <repo>\src\SCAR\verifier.py
    SCAR's own per-file grader (P1–P6 -> CONFIRMED / PARTIAL / REJECT), using
    pikepdf (structural: dangling refs, stream decode, object-stream decode,
    page-tree resolution) and PyMuPDF (render + optional ground-truth pixel
    hash). This is where SCAR's render-hashing originates; the scorer above
    applies the same rendering idea uniformly across all six tools.

SCAR_internal_content_aware_dedup.py.txt
    Source: <repo>\src\SCAR\dedup.py
    SCAR's content-aware dedup cascade (SHA-256 -> per-page render hash ->
    object-set containment). Note: for cross-tool fairness the result tables
    apply the scorer's own duplicate logic to ALL tools' raw output, not just
    SCAR's — so SCAR is not credited for a dedup step the baselines never ran.

-------------------------------------------------------------------------------
 DATASET-SIDE
-------------------------------------------------------------------------------

DATASET_pdf_xref_encoding_classifier.py.txt
    Source: <repo>\src\generators\classify_c1.py
    Classifies each source PDF by its real cross-reference encoding (classic
    xref table vs. PDF 1.5+ compressed xref-stream; Cohen-readable vs.
    Cohen-blind). Feeds the capability-gap breakdown (why the classic 2007
    carver scores 0 on xref-stream-only PDFs).

===============================================================================
 NOTE ON EVIDENCE PRESERVATION
 These .txt copies, the source scripts, all disk images under dataset\, all
 raw carved output under results\full_carved_output\ (including each SCAR
 run's _work\ folder with its .map files and evidence_hash.log), and the
 scored CSV are all retained as evidence. None are deleted.
===============================================================================
