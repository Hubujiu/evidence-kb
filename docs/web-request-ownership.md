# React workbench request ownership

Every operation belongs to one immutable identity, scope and view generation. Reconnecting invalidates the identity before its first request. Returning to the same token, scope or view does not restore an old generation. Parent state commits recheck ownership independently of component unmount timing.

The workbench passes AbortSignal to fetch, but correctness does not depend on cancellation support: every follow-up HTTP request and every result, error or resource publication checks ownership. An operation that already reached the server is not assumed rolled back by abort. In-flight mutations are not automatically retried.

Scope changes remount the workbench and dispose its resource owner. Preview URLs are created only by a current operation and revoked on close, replacement or unmount. Download URLs are tracked until revoked.

The mandatory project checker includes eight deterministic owner contracts. Browser acceptance additionally delays response bodies while ignoring cancellation, then switches identity or leaves the workbench and releases the old response. These tests exercise the actual React entry, not the retired console.

This change does not change backend authority, budget settlement or COMMIT_UNKNOWN semantics. It must be integrated with their reviewed implementations separately.
