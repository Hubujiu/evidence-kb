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

## Controlled commit-stage observations

PR73 head `f71bba77060186aca14f4ab54ad70610c36d0690`, run
`41558843828`, again failed Windows Python 3.12 in lexical `writer.commit()`.
The file snapshot does not identify the failing native syscall. Lexical files
having nonzero sizes does not prove every flush succeeded. Pages files being
empty is consistent with its later commit not yet being reached. The old
PR41/PR51/PR73 failures remain failures, regardless of later probe results.

The pinned Python and Rust Tantivy versions are 0.27.0. The official binding
converts native errors to `ValueError(err.to_string())`; Python cause inspection
cannot recover the discarded Rust error chain or path. Rust commit propagates
both indexing-worker errors and metadata-publication errors. Windows directory
sync is a no-op, while metadata publication uses tempfile write/flush/sync and
persist. These observations do not identify which operation failed.

Sources: [binding conversion](https://github.com/quickwit-oss/tantivy-py/blob/0.27.0/src/lib.rs),
[commit pipeline](https://github.com/quickwit-oss/tantivy/blob/0.27.0/src/indexer/index_writer.rs),
[atomic filesystem writes](https://github.com/quickwit-oss/tantivy/blob/0.27.0/src/directory/mmap_directory/mod.rs),
[managed-directory locking](https://github.com/quickwit-oss/tantivy/blob/0.27.0/src/directory/managed_directory.rs).
The claim of an unprotected managed-path save in upstream
[issue 2847](https://github.com/quickwit-oss/tantivy/issues/2847) is not supported
by this pinned source: the write guard remains live through the save. Historical
[issue 587](https://github.com/quickwit-oss/tantivy/issues/587) does not establish
antivirus or another process as the cause of our failures. No security tool is
disabled and no native failure is retried.

Failure-only notes retain stat-only observation and explicitly report
`not_sampled_active_writers` for metadata contents. A failed commit can leave
another native writer active, so catching the Python exception is not a safe
point to open either channel metadata. The controlled probe reads only the two
known metadata filenames in the just-completed channel after its successful
`wait_merging_threads`. It never reads the other still-active channel. Each read is capped at 64 KiB
plus one overflow-detection byte. Only opstamp and segment/managed-path counts
are reported, never raw JSON, schema, document data or managed-path strings.
Oversized, malformed, inaccessible, linked/reparse or concurrently replaced
files produce status labels, without retry. Identity checks happen before
reading and after reading. They detect observed replacements; these diagnostics
are not a general hostile-filesystem sandbox or proof of an atomic snapshot.
The original fixtures still do not sample successful stages. Before/after commit
observations contain only the phase, case, channel and a not-sampled status.
No platform-specific opener is introduced. Python ordinary Windows file opens
do not share delete access, so opening active metadata could itself interfere
with Tantivy atomic replacement. The smaller post-wait-only scheme avoids that
observer-induced conflict. See [CPython discussion](https://github.com/python/cpython/issues/88221)
and [Microsoft sharing semantics](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).

The new single discovered contract executes A (empty index), B (one synthetic
document), then C (the production dual-index stage with synthetic inputs), once
each. The first failing case stops the sequence and rethrows the same error;
no later case or wait step runs after a failed commit. A test-only forwarding
wrapper records at most 12 observations: before commit, after commit and after
wait, for the four writers. All native calls retain their original arguments,
return values and single invocation. The production implementation and native
dependency are unchanged. Successful probe observations are printed into normal
CI logs; failure observations are exception notes. Private local logs are not
uploaded as repository files.

A has no document-segment flush work. Comparison with B and C can distinguish
triggering workloads but cannot prove the failing OS operation or responsible
process. The added observation IO can affect timing, so a passing controlled
probe is not evidence that the original Windows error was repaired. Metadata
changing during observation is a valid unavailable observation, not a reason to
retry or fail the underlying native operation.

The mandatory source gate binds the reviewed diagnostic and probe ASTs. Updating
that binding requires reviewing the whitelist, byte/event bounds, sharing and
identity checks, single-pass ordering, forwarding calls and original-error
preservation. Mutation contracts reject widened reads, unbounded reads, raw
metadata output, removed identity checks, repeated commits, missing cases,
wrong stage labels, active-writer content reads, cross-channel reads after wait,
and swallowed failures. Behavioral contracts separately
check those guarantees with deterministic failures; source binding is not a
substitute for execution.

Dependency evidence starts from public PR41 `64f8916`, whose tree is
`320342aa465594817a50f939c7ee3bd45a3b9c8a`. Local `7718df4` has that same tree
but different history; publishing must use the public parent, not push private
local ancestry. PR73's original base is that public PR41 commit; its original
head and CI evidence remain immutable. Any later combined validation must record
the new base, original feature delta, exact head and tested tree separately.

The initial local-only candidate sampled contents during active commits. Its
Linux checks passed, but review identified the Windows delete-sharing risk
before publication. That candidate was not run on Windows or published; the
revised protocol removes active content reads. Its old local PASS is not reused
as validation of this revised candidate.

### Python AST presentation is not the binding format

The first published probe commit `13f0f6e` failed the Python 3.13 architecture and
mutation contracts: its fixed fingerprints used `ast.dump`, whose Python 3.13
default presentation omits empty fields that Python 3.12 prints. This diagnostic
gate defect is separate from the native Windows failure. The original failed
CI is retained. The replacement reuses the existing formal `source_digest` helper, which sets
`show_empty=True` on Python 3.13 and retains the Python 3.12 default. There is
no second hash format and no list of old version-dependent hashes. The pure-stdlib lint contract
runs on both supported Python versions, verifies a fixed explicit-field example,
and rejects behavior mutants. It is separated from native probe execution so
lint compatibility can be checked without installing another native wheel.
See [Python AST documentation](https://docs.python.org/3/library/ast.html#ast.dump).
