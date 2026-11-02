# Page conversion and explicit OCR recovery — bounded audit

Four confirmed regressions are fixed here. PR50 remains the separate parse-to-
ingestion admission model; this directory does not claim all parsers are correct.

## Mature implementations consulted first

- [Docling conversion result](https://docling-project.github.io/docling/reference/document_converter/)
  distinguishes SUCCESS, PARTIAL_SUCCESS and FAILURE and retains attributable
  conversion errors. An exact cached partial result must retain that distinction.
- [OCRmyPDF time/image limits](https://ocrmypdf.readthedocs.io/en/stable/advanced.html#time-and-image-size-limits)
  handle page failures/skips individually and preserve the remaining document.
  This motivates isolating fallback attempts, not inventing native text when a
  selected replacement parser fails. Existing fail-closed replacement semantics
  remain unchanged.
- [OCRmyPDF batch documentation](https://ocrmypdf.readthedocs.io/en/stable/batch.html)
  is background only; this change does not start an automatic retry daemon or
  repeat unknown physical model requests. `augment_ocr` recovery is a separate,
  explicit caller invocation, with completed pages excluded.

## Findings and source mapping

| Confirmed trace before fix | Repair | Required source lint |
| --- | --- | --- |
| A real one-page PDF gets synthetic PARTIAL_SUCCESS. First parse is PARTIAL; exact StageCache hit reports READY and drops partial conversion. | `_accept_page_result` is shared by live, cached and fallback admission and retains conversion status after exact page-hash validation. | All three call sites plus partial-status and identity guards. |
| Two real PDF pages have empty Docling OCR exports. Direct OCR failure on page 0 aborts the loop, so page 1 is never tried. | Fallback identity discovery remains shared; every page attempt has its own try/except and diagnostic history. | Per-page guarded loop; no break/return on page failure; failed fallback attempt retained. |
| Two-frame TIFF: frame 0 succeeds, frame 1 fails. Explicit recovery repeats frame 0 and appends duplicate evidence. Calling the completed result repeats everything again. | `_frames` excludes OCR_COMPLETED and LOCAL_PAGE_PARSE_COMPLETED identities for both PDF and image inputs; zero-work recovery is an exact no-op. Successful recovery clears only its own OCR failures and native review warnings. | Completion-set derivation, PDF subtraction, image skip, no-op guard and scoped warning cleanup. |
| Successful local page completion is omitted when a sibling fails. Real ingest then calls vision OCR on the successful page again. | Publish LOCAL_PAGE_PARSE_COMPLETED independently for each successful page, even in a partial ensemble. Preserve unresolved external layout/table capability failures as PARTIAL. | Completion loop must be at assembly level, not guarded by document-wide failure status. |

Version strings change to `page-layout-local/4` and `luna-vision-ocr/0.3` so changed
assembly semantics create new snapshots. External parser payload/cache protocol is
unchanged: the cached artifact was already correct; its interpretation was not.

## Finite models and real controls

- **PageCache**: one immutable page payload, two source identities, two outcomes,
  cold admission then one exact cache read. `NoFalseReady` catches the prior
  PARTIAL_SUCCESS-to-SUCCESS promotion. `IdentityBound` also has a seeded unsafe
  guard-removal control; that control is not a newly discovered product bug.
- **PageFallback**: two primary empty pages, one fallback per page, allowed/blocked
  policy, success/failure outcomes. The legacy broad catch violates
  `IndependentPages`. Primary failure history stays present after recovery.
- **OCRRecovery**: two page identities, two explicit invocations, boolean success
  responses and any subset of initially completed local pages. `NoCompletedPageCall`
  forbids repeated calls even when their new responses fail. A lost-handoff control
  removes local completion; a repeat-call control removes explicit-resume filtering. Completed-page exclusion prevents repeated successful evidence;
  output is canonically assembled by page identity. Geometry/ordinal ordering is
  deliberately abstracted, and separately exercised against real PDF raster input.

Each legacy configuration reproduces a named safety violation (TLC exit 12), not a
syntax/runtime error. Each corrected model exhausts its finite graph. Three
`Success` configurations deliberately violate named unreachability assertions to
witness warm partial reuse, later-page recovery after an earlier failure, and
successful explicit OCR completion. These are reachability checks, **not** bugs
or fairness/eventual-progress proofs.

`formal_state_machines.registry_violations` invokes `page_recovery_rules` in both
ordinary and formal gates. Nine source mutations refresh every model source
hash and are still rejected by mandatory structural lint. This targeted AST
lint is not a general semantic analyzer; hashes alone are only review-drift gates.

## Executable contracts and abstraction boundaries

The ordinary discovered module `page_recovery_contracts` covers exact real
StageCache reuse; per-page fallback failure isolation and attempt history; TIFF
partial recovery and no-op repeats; actual PDF raster OCR with failed first page,
reordered boxes, immutable source links and regenerated snapshot references;
wrong-page source hash rejection without losing the verified sibling; explicit
continuation after the per-invocation page limit; failed and
policy-disabled native inputs; and all nine hash-refreshed lint mutations.

The contracts use synthetic documents decoded by actual local PDF/TIFF
implementations and deterministic synthetic model/worker responses. They require
no external provider or new model installation. These are component behavior
contracts, not human business gold, OCR accuracy, online latency or production RAG
delivery metrics. Unresolved external layout/table capabilities remain PARTIAL
after vision transcription; transcription alone cannot establish layout success.

Run the two required gates independently:

```sh
uv run --no-sync python scripts/check_project.py
uv run --no-sync python scripts/check_project.py --formal-only
```

## Remaining obligations

The registry remains **partial**. Arbitrary malformed PDFs, third-party model
quality, page geometry outside these real fixtures, resource/power-loss behavior,
provider billing/retry settlement, unlimited page counts and cross-model
composition with authority/publication/cache revocation are unproved. A failed
selected replacement parser is not silently substituted with native evidence.
