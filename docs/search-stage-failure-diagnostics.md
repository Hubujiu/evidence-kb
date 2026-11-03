# Search staging first-failure evidence

PR41 run `41502959771` failed in native Tantivy `writer.commit()` with Windows
access-denied error 5. The trace did not identify the adapter, lexical/pages
writer, path, or native operation. That original failure remains unresolved;
new passing observations do not prove the native issue was repaired.

The `SearchRouterContracts.stage` and `EmbeddedSearchContracts._stage` fixture
wrappers annotate an existing exception.
It samples after the original stage call fails, identifies the original
`EmbeddedSearch.stage` frame by code-object identity, and maps its current
writer by identity. PEP 678 notes preserve the same exception and cause; a bare
raise preserves the original failure. Repeated annotation is ignored, and
diagnostic faults cannot replace the exception. No commit is retried.

Metadata is bounded to 64 entries and two directory levels under the fixture's
owned staging directory. Notes contain relative names, sizes, modification
times, mode/attribute flags, and the writer label, never contents, ACLs, global
process data, or absolute temporary paths. Outside-root paths and observed
symlink/reparse directories are rejected; entries are statted without following
links. Sampling is read-only and does not reopen the index, change permissions,
clean up files, run garbage collection, or alter production writer behavior.

The mandatory source-quality gate checks the fixture's exact one-call,
failure-only-note, bare-raise structure. Synthetic contracts reject retry,
early-sampling, and replacement-exception mutants, and verify single commit,
exception/cause identity, metadata limits, path boundaries, and diagnostic-fault
containment. Existing search assertions, deadlines, and production code are
unchanged. Metadata can narrow a future failure, but cannot by itself name the
failed native OS operation or attribute it to another process.


## Embedded fixture coverage after PR51

PR51 run `41527259006`, Windows Python 3.12, failed the original
`test_closed_immutable_readers_release_generations_for_immediate_removal` at
`EmbeddedSearch.stage` line 137, `writer.commit()`, with native access denied.
That run remains FAIL and the native cause remains unknown; this change only
extends first-failure observation. A subsequent passing run is not a repair.

All five existing embedded fixture call sites now delegate through one thin
wrapper, with original receivers, arguments and invocation counts unchanged:

1. `setUp`: initial immutable generation.
2. `test_exact_vectors_and_seal`: expected refusal to replace publication.
3. `test_failed_staging_not_visible`: expected invalid-vector refusal.
4. `test_pages_do_not_change_block_scores`: original block-only generation.
5. `test_closed_immutable_readers_release_generations_for_immediate_removal`:
   original four-generation loop, all original search, double-close and
   immediate physical-removal assertions retained.

Only the current fixture's `Path(self.folder.name)` is supplied to the unchanged
helper. No production search/writer code, retries, cleanup, garbage collection,
permissions, global process inventory, ACL collection or tracing software is
added. The mandatory guard binds all five call expressions and permits exactly
one underlying stage invocation in the failure-only wrapper. It rejects setup
or loop bypass, a dropped call, repeat invocation, pre-failure sampling, widened
root and exception replacement. New deterministic contracts inject a commit
failure through the actual production stage path from both real fixture setup
and the generation loop, asserting original exception identity/cause/args and
one commit. Real successful setup and all four loop generations run with a note
function that would fail if sampled, preserving the original assertions.

The first new synthetic writer fixture omitted add_document, so its two cases
failed before commit with AttributeError; this test-harness error is retained at
`/tmp/evidence-embedded-diagnostics-targeted-20261102T220545Z`. The corrected
fixture implements that operation without changing production stage behavior;
its later result is recorded separately. Neither synthetic failure is claimed
to reproduce the real Windows denial.

Dependency isolation: this feature continues the exact PR41 diagnostic tree.
Local PR51 composition cherry-picks only PR41's diagnostic commit plus this
extension, not its storage-protocol feature ancestry. PR51 itself is not changed
or rerun. The PR41 candidate has its own frozen mandatory check and new CI;
composition evidence is targeted plus mandatory source guards, not another
remote combination PR or a claim that PR51's failure has been repaired.
