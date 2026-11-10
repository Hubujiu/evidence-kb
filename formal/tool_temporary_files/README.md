# Formal tool temporary-file ownership

Two independent Java executions may resolve the same standard TLA+ module concurrently. The pinned 1.7.4 jar reads `java.io.tmpdir` into `FilenameToStream.TMPDIR`; `SimpleFilenameToStream.read` builds that directory plus the module name and calls `deleteOnExit` before writing the extracted module. A JVM exiting can therefore delete a standard module another JVM still needs when they share the default directory. This was observed as TLC exit 150, missing `/tmp/Naturals.tla`, during overlapping full checks.

`ToolTemporaryIsolation` models two executions preparing, loading and deleting one standard-module file in their namespace. The positive configuration uses distinct namespaces; SharedToolTemporary reproduces the shared namespace and violates ActiveFilesRetained. Two negated reachability predicates witness overlapping loaded executions and one surviving the other's exit.

The implementation gives every TLC and SANY invocation an exclusively created absolute `java-tmp` directory beneath its unique run output. The existing output-directory creation rejects reuse. Source lint checks both command construction and directory ownership; offline contracts inspect actual invocation commands, and an additional real concurrent TLC check validates the boundary. The original failed run remains an error, never a successful model counterexample.

Bounds: two JVMs, one representative standard module, one lifecycle per JVM. Filesystem isolation, truthful Java property handling and no external deletion remain assumptions. The model abstracts extraction as one action; it does not prove crash durability, arbitrary filesystem adversaries or JVM implementation refinement. Repeated failing model checks are not retried or silently hidden by the runner.
