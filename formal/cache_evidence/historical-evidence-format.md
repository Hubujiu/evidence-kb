# Historical evidence format

The tracked JSON files with schema `historical-tlc-summary/1` are compact
historical records, not the live runner's report schema and not new verification
results. They preserve each run's identity, verdict, actual exit code, timeout,
duration, expected invariant/property, explored-state counts and original
command. `raw_log` points to the unmodified TLC output beside the summary.

Each `input_checksums` file uses the standard SHA256SUMS text format. Its paths
are repository-relative inputs **at that historical run**, not promises that the
current tree has those bytes. Each run has its own manifest; in particular the
original receipt model and the later cancellation model retain different hashes.
Do not replace historical hashes with current hashes to make a check pass.

This format applies to the histories under both `formal/auth_session/evidence`
and `formal/cache_evidence/evidence`. The preceding integrated verification
summary and earlier stage summaries retain their original scope, including old
dependency drift. The raw baseline failures are retained. One generated
32-character receipt nonce in `cancellation-baseline.log` is explicitly replaced
with `[REDACTED_SYNTHETIC_RECEIPT_NONCE]`; it came from the synthetic in-process
fixture, not a provider credential. No assertion result was changed.

The original [PR44 candidate](https://github.com/Hubujiu/evidence-kb/commit/6ee22e7de17d1d8187d5e973148edc6672dd269e)
failed secrets job `125500012670`: gitleaks 8.30.1 reported fourteen
`generic-api-key` findings in repeated `input_sha256` maps. Each value was
verified as the SHA-256 of a public TLA/CFG source Git blob, not a credential.
`evidence/original-secrets-failure.json` preserves the finding locations and
classification without copying matching values. The compact summaries and
ordinary checksum manifests remove redundant machine-output copies; they do
not encode or conceal secrets. No scanner configuration or allowlist is changed.

`run_model`, `run_formal`, and `live_suite` remain unchanged. New formal runs still
produce complete `result.json` and `summary.json` records with `input_sha256`,
copied model inputs, raw logs and the existing evidence checks in their unique
output directory. The tracked historical summaries are not consumed by those
checks and must not be supplied as a substitute for live-run evidence.

The frozen candidate's final additional formal run passed twenty model/control
runs. Its ordinary full project run failed separately: plugin-process and
foundation integration timed out (the former also reported
`STORAGE_HANDLE_CLOSED`), and eighteen contract errors reported a missing
`socksio` dependency while reading the environment's SOCKS proxy. These failures
remain at `/tmp/evidence-auth-cache-full-project-20261102-1926`; this documentation
cleanup does not reclassify them as passes or rerun that full gate.
