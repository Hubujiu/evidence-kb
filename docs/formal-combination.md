# Formal feature combination (work in progress)

This integration tree combines the public feature inputs in `formal/combination-inputs-v1.json` over the immutable PR31+32 dependency tree. It is a separate verification candidate, not a replacement for the independent feature PRs, an all-owner proof, a main merge, or a deployment.

## Assembly contract

Each feature is applied as its published base-to-head delta in dependency order. Machine records, source bindings, model runs, negative controls, limitations, and discovery metadata are combined by identity. No private feature ancestry is merged or published. Historical execution logs and reports are excluded from the publication projection. Two previously public browser source-review JSON files remain in their exact original bytes with explicit authorization because the unchanged mandatory browser source-drift contract consumes them. New execution logs and reports remain private.

The input-preservation contracts compare exact run IDs and definitions, TLA/CFG bytes (only checkout CRLF is normalized), retained contract case identities, and independently reviewed shared gate entry points using the existing cross-version Python AST digest. These checks prevent accidental omission or overwrite; they do not turn counts or source fingerprints into semantic coverage. Legitimate later feature inputs require an explicit manifest and shared-entry-point review.

## Reviewed shared boundaries

- Provider runtime retains separate circuit, receipt, and tool-policy owners. Receipt database checks remain under `provider_receipts`; remote pre-send checks and persistent permission/checkpoint checks both remain under `provider_admission`.
- PR34's final metered transport keeps pre-dispatch known-unsent handling outside dispatched observation and one-shot settlement. The older capture binding is not restored.
- Cross-owner bindings use the exact published source symbols from PR36 (receipt begin/finish), PR42 (canonical error-field module), PR44 (model receipt and public error projection), PR45 (knowledge turn), and PR53 (403 embargo before settlement). No novel combined production symbol fingerprint was needed.
- The shared runner retains JavaScript discovery, every owner lint, official SANY review before TLC, and the two separately reported PR35 `OBSERVED_LIMITATION` outcomes. The known authority-check-to-session-commit gap is not declared fixed.
- Foundation integration retains delivery-status, receipt process/crash, acceptance preparation, and the exactly-once browser asset ASGI case. Host fixture allowlists and browser source gates remain mandatory.
- Rust feature changes retain their public source/test bytes; native verification must build this combination's own library.

## Combination-only fixture adjustments

The limitation-report unit fixture now includes an explicitly synthetic SANY export report so the PR35 evidence contracts can exercise the PR60 mandatory evidence schema. A failing inline review remains a rejecting mutation.

The pre-send lint fixture now supplies the additional permission/checkpoint and physical-settlement owner sources required by the combined mandatory registry path. Existing refreshed-hash mutants and constant-field-loss assertions are unchanged; no mandatory check is skipped.

## Subsequent owner inputs

PR66 adds independent acceptance binding/publication and client execution models. Its scenario-file fix routes split, scenario and descriptor publication through immutable-equality reuse before atomic creation; the pre-existing prepare/receipt guards remain. PR67 adds Platform upload/result snapshot and Auxiliary OCR upload models and mandatory source/witness lint without production owner changes. Both are applied only as their declared public dependency deltas.

## Validation boundary

The first focused run exposed these two fixture dependency conflicts and a native-dependent case selected before a combined library existed. Its failures are retained privately, not converted into passes. Corrected fixture contracts passed a focused rerun. Ordinary full checks, formal-only execution, and exact remote CI remain required and are not claimed here. Additional independently identified state owners are still pending; this candidate is not final acceptance.

PR68 adds SDK gateway request/recovery/shutdown ownership and query/index cleanup. PR69 adds native multiblock assembler/replay projection models and contracts without production changes. PR70 adds the public `CanonicalPageIngestion.run` protocol (including two-source/batch processing, receipts/capsules/readback, optional build and retained costs); absent CLI wiring does not exclude it. PR71 adds Python executor/context leases and HTTP lifespan cleanup. Their exact public deltas are present, but combined native/runtime verification and exact CI are not thereby established. PR72 adds persistent migration and document-revision ownership with migration cleanup repair; PR73 adds global native storage-session ownership and exact native tests without changing production protocol logic. Their ordinary gates and exact-head CI remain independently pending at this input freeze. External provider internals and arbitrary historical research scripts are not added to scope.

At this WIP freeze PR68 remains at its first public head. A newly reproduced recovery-provider scope mismatch (same payload/context but changed provider base URL) is an open obligation awaiting the author's follow-up; its earlier CI result does not close that gap. PR71's public nested-lease witness now records the real nesting transition. A separately observed dispatcher race is still under author investigation and is not claimed repaired in this frozen input.
