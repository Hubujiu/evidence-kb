# Exact-run development evidence

## First saved candidate

Local `5757cb9e3f59492255aa115924378662f4e59340`, public head `04e3c5c9fdae3159630a934be789370dbe8062b3`, tree `26198e0c74def0aff9578d1444c4103ea9c36f76`.

- Four original product reproductions fail before the fix (baseline.log). With the fix, all four pass and fresh replacement remains callable.
- Final focused suite: 42 PASS. Formal required stage: 16 expected PASS, including the explicitly labelled reachability observations. These do not substitute for ordinary/full validation.
- First ordinary local full: FAIL, 10 of 12 gates pass. Contracts discover 1075 tests, with 3 assertion failures and 18 errors, no skips. The errors are the existing HTTPX SOCKS transport construction / missing optional socksio environment issue. The **three assertion failures are our new lint wiring bug**, not environment failures: the new owner-specific lint also ran on generic tiny registry fixtures without storage source files.
- The local host gate separately failed the large parsed-document build with public AUTHORITY_UNAVAILABLE (503). A one-off cause-observation diagnostic on the unchanged production source later completed successfully in 177.984 seconds and observed no exception. It did **not** recover the original hidden cause and does not erase the original failure. No timing limits or retries were added.
- First normal CI [41535279558](https://github.com/Hubujiu/evidence-kb/actions/runs/41535279558) is retained FAIL. All four platform quality cells and integration each discover 1075 tests, with the same three lint-fixture assertion failures, zero errors/skips. Other gates, including their host checks, pass; the browser is not reached after the prerequisite aggregate fails. Formal and secrets pass. All six artifact archive digests match both API metadata and upload logs, with raw TLC frames and frozen input checksums independently verified.

## Lint wiring correction

The storage-specific AST review is now invoked when validating its registered `storage_registry` owner. Generic registry fixtures continue testing the generic validator without irrelevant files. The production inventory still contains that owner, and an added mandatory-hook regression injects a sentinel violation and requires it to be surfaced exactly once. Existing source-shape mutation checks still reject bypasses independently of source checksums. No production code, TLA model/configuration, bounds, thresholds, discovery or existing assertions changed in this correction.

Focused corrected lint/registry suite: 24 PASS. New exact-source formal/full and CI evidence will be reported separately; this document is not a declaration of final acceptance. No paid model, embedding or judge calls; no merge/deployment or CI rerun.
