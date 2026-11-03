# Offline provider fixture construction

The synthetic Google fixtures previously constructed a default HTTPX client before
installing their MockTransport. On hosts with a SOCKS proxy, HTTPX attempted to
construct a proxy transport and import optional socksio during fixture setup. This
prevented 18 subtest outcomes across five contract modules from reaching their
budget, authorization, recovery, and invalid-configuration assertions. Historical
failed runs remain failed; this change does not reinterpret or delete them.

The test-only `offline_client_construction` context injects a new fail-fast
MockTransport into each real HTTPX Client during the narrow construction window.
It forwards the original timeout and trust_env arguments. It does not edit process
environment, configuration, production defaults, dependencies or network policy.
It rejects explicit transport/proxy/mounts instead of silently replacing routing.
No request is allowed until the existing fixture installs its capture transport.
On setup failure, all clients created within the window are closed and the factory
is restored. On success, ownership remains with the original provider/host, which
closes or replaces the client through its existing lifecycle.

This follows [HTTPX's documented MockTransport injection](https://www.python-httpx.org/advanced/transports/#mock-transports).
In [HTTPX 0.28.1 Client construction](https://github.com/encode/httpx/blob/0.28.1/httpx/_client.py),
`allow_env_proxies = trust_env and transport is None`: an explicit mock transport
avoids constructing a real proxy transport without changing trust_env. The project
also already constructs real HTTPX clients through a controlled factory in its
model provider contracts.

Four construction sites use the helper: captured Google embedding jobs, model
review, model embedding review (also reused by knowledge model port contracts),
and the remote plugin configuration preflight fixture. No production constructors
or existing target assertions change. After setup, the real MeteredTransport,
BudgetLedger, ProviderAdmission and original receipt/recovery paths run as before.
Synthetic responses remain synthetic and do not demonstrate production RAG quality.

The mandatory architecture gate checks transport injection, fail-fast behavior,
cleanup, policy preservation and that each relevant constructor occurs inside its
controlled window. Mutation contracts reject missing injection, late/unprotected
construction, transport=None, policy overrides, environment mutation, fake clients,
successful placeholder responses and omitted cleanup. Behavioral contracts verify
real client identity, independent transports, unchanged environment (boolean only,
never values), fail-fast sends, cleanup and a captured metered request. External
network auditing remains unchanged and is installed before test discovery.

Full project and formal checks remain required; targeted checks are not full
acceptance. This fixture-only change introduces no product state-machine transition.
