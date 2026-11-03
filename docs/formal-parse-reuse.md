# Invocation-local formal source parsing

The registry still resolves and confines each source path, verifies that it is a file, and reads fresh UTF-8 text for every binding. Only Python parsing and symbol discovery for identical complete source text are reused inside one registry invocation. The scoped cache holds at most 128 entries and clears in `finally`, including on failure. It never uses path, timestamp, size, or a caller-supplied hash as authority.

This follows the standard-library [functools caching guidance](https://docs.python.org/3/library/functools.html#functools.lru_cache): memoize deterministic computation with hashable inputs, not side effects or public mutable results. Cached ASTs remain private to the digest callback. Callers receive only the existing digest string; public `python_symbols` continues returning independent mutable trees, and public `source_digest` remains uncached. Syntax failures are not cached. The existing Python-version-aware AST dump remains unchanged.

No filesystem snapshot is asserted. A content change between two reads uses a different parse key. Path resolution and confinement still occur before every read, so this change neither adds nor removes the existing filesystem TOCTOU boundary. Symlink replacement, missing files, and permission failures must still be checked on subsequent calls.

The mandatory source-quality lint fixes these narrow cache ownership and fresh-read boundaries. Contracts cover unchanged I/O call counts, repeated symbols, same-size/mtime changes, distinct roots, symlink escape, exceptions, cleanup, independent public ASTs, and exact cached/uncached violation-list equality. Existing semantic mutation and refreshed-source-hash contracts remain mandatory.

This change does not modify model inventories, negative controls, worker sharding, deadlines, or assertions. Linux profiles established redundant parsing, not a Windows speed guarantee. The original PR74 Windows 170-second timeout remains a recorded failure until a new exact-commit Windows run passes. Passing finite model checks does not prove the absence of all implementation bugs.
