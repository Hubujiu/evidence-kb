# Search staging first-failure evidence

PR41 run `41502959771` failed in native Tantivy `writer.commit()` with Windows
access-denied error 5. The trace did not identify the adapter, lexical/pages
writer, path, or native operation. That original failure remains unresolved;
new passing observations do not prove the native issue was repaired.

Only the `SearchRouterContracts.stage` fixture annotates an existing exception.
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
