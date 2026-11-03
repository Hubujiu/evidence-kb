# Offline worker bootstrap and audit sealing

## Observed failure and primary implementation sources

PR #32's first CI run `41487295126`, head `8003c1dad81195928cdac057a2525ea884e99922`,
failed discovery in one Windows/Python 3.13.17 shard. The retained traceback is:
SQLAlchemy import → `platform.machine()` → `uname()` → `win32_ver()` →
`_syscmd_ver()` → `subprocess.check_output(..., shell=True)` →
`OFFLINE_CHECK_FORBIDS_subprocess.Popen`. The other three shards passed; this
does not establish why that worker took the fallback path.

The pinned [CPython 3.13.17 platform implementation](https://github.com/python/cpython/blob/v3.13.17/Lib/platform.py)
uses WMI first for Windows version information. An unavailable WMI query can
reach the `ver` shell fallback. `machine()` returns `uname().machine`; `uname()`
caches its real result. Reading `machine` does not force the separately lazy
`processor` property.

The other primary source inspected is the actual publisher-installed
`sqlalchemy/util/compat.py` from the `SQLAlchemy==2.1.6` distribution locked by
`uv.lock`: line 45 eagerly reads `platform.machine()` while importing SQLAlchemy.
The installed file's SHA-256 is
`f7fc4c12d5c9b1b7b921680a6ea453f6ee9b7609aae3b73fc5773816d166c5c7`, verified against
the distribution's `RECORD`. The locked publisher source archive is
[`sqlalchemy-2.1.6.tar.gz`](https://files.pythonhosted.org/packages/36/dc/9addbf490110b5727380059ee542ad8353bdcee45a37ba6a5e0976966ae0/sqlalchemy-2.1.6.tar.gz),
SHA-256 `499eae5c9051efe96cb9a73ec2fbde8ffa4186f102b9094ebab8fcc9d3b99f52`.
No dependency upgrade or vendored SQLAlchemy patch is involved.

## Controlled state boundary

1. **BOOTSTRAP:** the trusted checker reads real stdlib `platform.machine()` once.
   The earlier Windows failure involved its standard OS metadata fallback.
   No product, SQLAlchemy, or contract module is imported to warm the cache.
2. **SEALED:** immediately install the unchanged `deny_external_io` hook. There is
   no subprocess allowlist, fake machine value, environment-derived replacement,
   direct cache assignment, or temporary relaxation of the audit hook.
3. **DISCOVER:** archive the observed machine and boundary states in each worker's
   `worker-bootstrap.json`, then import/discover/run contracts or scorer modules.
   Socket/DNS/UDP and process-launch audit events remain denied. If bootstrap or
   sealing fails, discovery does not start and the gate cannot pass.

Prewarming `machine()` is sufficient for the observed SQLAlchemy import path,
not every possible future platform API. Explicit cache invalidation or an
unrelated operation that needs a subprocess after sealing still fails closed.
This is accident prevention for the trusted test runner, not a malicious-code
sandbox or a proof of every dependency's behavior.

## Mandatory checks and limits

The architecture/source-quality gate applies `offline_bootstrap_violations` to
the checker AST. It checks the narrow metadata/seal helper, worker entry order,
startup imports, and absence of test discovery in the unsealed coordinator.
`offline_bootstrap_contracts.py` rejects deleted/reversed sealing, pre-imported
project/dependency code, eager bootstrap defaults, and discovery before sealing;
formatting and unrelated post-seal edits remain accepted.

Runtime contracts verify that actual process/network audit events are rejected
after sealing and that failures prevent discovery. The cold-metadata fallback
test is explicitly synthetic; passing Linux tests does not establish Windows
acceptance. The historical Windows failure remains recorded; the current
project and CI support Linux only.

Worker/coordinator deadlines, contract discovery completeness, per-module
nonzero counts, and skip/expected-failure rejection are unchanged. This change
does not address the separate host-integration timeout and does not increase
its 180-second limit or skip any process-cleanup assertion.
