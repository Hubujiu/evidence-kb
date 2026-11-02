# Session authentication: read snapshot and own expiry

Historical summaries and per-run SHA256SUMS follow the shared
[evidence format](../cache_evidence/historical-evidence-format.md).

This bounded model covers `SessionAuthentication`, not every HTTP or static
authentication mechanism. No product defect is claimed in this stage.

The mature references are [OWASP server-side session expiration](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html#session-expiration)
and [Redis EXPIRE](https://redis.io/docs/latest/commands/expire/): session timeout
must be enforced by the server, and an expiring store may remove records at access
or during cleanup. The implementation additionally checks the record's own
`expires_at` after `get`; it does not assume the backend has already removed it.

| Model | Implementation / observation |
| --- | --- |
| `Issue(t, actor)` | Validate actor/TTL, create token, store actor+tenant+expiry under token hash |
| `records`, `owners` | Trusted store value and immutable actor/tenant tuple |
| `Read` | Atomic `store.get` snapshot, or backend failure |
| `snapshot` | Missing, valid, malformed, or failed read; parsing failures reject |
| `Validate`, `checkedAt` | Validate schema, construct actor, check own expiry after the read |
| `Revoke` | Atomic delete; also abstracts TTL deletion or storage loss |
| `Corrupt` | Malformed record, not a well-formed forged actor from a compromised trusted store |

**Linearization is explicit.** Revocation wins if it precedes the store read.
If an unexpired valid record was already read, a later revoke does not rewrite
that local snapshot. Own expiry is checked at validation, not just before an
arbitrarily long store read. A returned authentication decision is not a promise
that authorization remains valid at HTTP response delivery. The regression
deliberately interposes revoke and expiry after obtaining the stored bytes to
verify both cases. Other tests cover the same principal across two tenants and
two principals in one tenant, corrupt records and backend failure.

Bounds: two tokens, four tenant/principal tuples, one authentication operation,
issuance at time 0, expiry at time 2, and clock values 0–3. Issuance and deletion
are atomic store effects. No hash collision, token reuse or clock rollback is
modeled. `SkipExpiry=TRUE` is a deliberately seeded variant, not existing product
behavior: it must violate `OwnExpiryEnforced` (exit 12). Positive invariants are
checked against captured validation time, not a later clock value.

No liveness/fairness claim, cryptographic proof, database consistency proof or
Python-to-TLA refinement proof is made. Distributed clocks, floats, acknowledgement
loss, trusted-store compromise, unbounded token space and transport remain open.
The `partial` registry entry lists these limits and binds the actual sources and
deterministic regressions. TLC evidence is retained in `evidence/`; existing logs
do not replace rerunning the formal gate after changes.
