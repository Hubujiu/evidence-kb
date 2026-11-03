# Acceptance output workflow dependency snapshot

This immutable subset starts at public PR #34 `b6134a6332ff3ae96294bd8a36658c7d413b2689`
and applies only the true-base code deltas of PR #36 `038e8b2b6f0036e4dc0221eaafe962a456d2c295`
and PR #46 `ff6d9637f1e94944e85e72f37bdfa57ac2b68ce5`. No private commit is a publication parent.

The physical capture/settlement implementation remains PR #34. Receipt begin/finish
anchors are explicitly reviewed against PR #36; SQLite locking and its mandatory
process contracts remain. Registry machines are combined by ID, and the foundation
stage retains delivery status, receipt process and preparation ownership suites.

Old raw execution evidence referenced by the inherited PR #46 README remains in
that original public PR; it is not copied into this subset. New execution outputs
are private, not source artifacts. This snapshot does not establish full combination
coverage, change provider semantics, authorize retries or call any real provider.
