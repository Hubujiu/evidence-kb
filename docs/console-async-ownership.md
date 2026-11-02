# Console asynchronous ownership

The document-management console's executable owner is `kb_platform/console.mjs`,
loaded as an ECMAScript module by `console.html` and served by `/console.mjs`.
Markup and styling are unchanged. The initial extraction preserves the old
request behavior so controlled browser scenarios can first establish its failures.

## Source gate

The unified project checker runs `browser-source-lint`: Python's standard
`HTMLParser` checks that this entrypoint loads exactly its mapped module and has
no inline scripts, event handlers, `srcdoc`, JavaScript URLs, embedded executable
documents or base-URL changes. Node's actual `--check` parses the `.mjs` file using
ECMAScript module grammar. CI reuses the existing pinned Node 22.23.1 action.

Formal `javascript-module` bindings are restricted to complete `.mjs` files.
Their SHA256 normalizes CRLF to LF, as Rust module bindings do. This is source
review drift detection, not JavaScript AST equivalence or source-to-model proof.
The placement checks are an architecture guard, not a general HTML sanitizer or
security sandbox. Semantic ownership needs separate models and browser regressions.

## Reference boundary

[React's official data-fetching example](https://react.dev/reference/react/useEffect#fetching-data-with-effects)
guards against superseded responses during cleanup. The same request-ownership
principle applies to plain JavaScript without adding React.
[MDN's AbortController documentation](https://developer.mozilla.org/en-US/docs/Web/API/AbortController/abort)
describes canceling fetch and body consumption. Aborting a client request does not
prove a server mutation was rolled back; this work does not add automatic retries.

Only citation selection, asynchronous evidence rendering and preview URL ownership
are in scope. Agent draft ownership, session settlement, periodic polling and visual
features are not changed by this batch.

## Reproduction boundary

The reproduction-only commit retains the exact JavaScript extracted from
`c8ef758:kb_platform/console.html`: SHA256
`3ceb6599145a3fc70d26d7fb726a88b6b99b85f0093c72737781b715b981403e`.
`e2e/console-state.e2e.js` uses the existing deterministic browser harness with
controlled fetch/header/body promises, real DOM nodes and native object URLs.
The normal browser integration runs these cases in addition to its original two
knowledge tests; there are no model or provider calls.

A standalone synthetic-only path is available without a product server:

```sh
uv run --no-sync python -m kb_platform.rag_testing.console_browser_integration \
  --output /new/unique/output
```

An explicit `--chromium /path/to/chromium` uses an already-installed browser
through the harness's supported CDP interface, a fresh owned profile and loopback
port, without disabling its sandbox. Its version is reported separately from the
CI-pinned browser. The local Chromium 155 attempt did not reach the tests because
the environment rejected its process-singleton socket. No browser baseline or
fixed pass is claimed from that attempt. The pinned Chromium 156 CDN returned an
HTML `Site Unavailable` response; no alternate download was attempted.

The first PR38 CI baseline reached the console runner only after the original
knowledge-browser cases passed, but the e2e CLI rejected its output directory
outside the e2e project root (`INVALID_CONFIG`, exit 2). No console assertions
executed, so that run is infrastructure evidence, not a race counterexample.
The harness correction uses the existing `e2e/.artifacts/<unique-id>` convention
and copies artifacts into the requested report directory even after failure.
It leaves the five desired browser assertions and legacy module bytes unchanged.
