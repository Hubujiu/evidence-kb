# Console build and publish ownership

The diagnostic document console performs two mutations: build a candidate and
publish the returned build. Both belong to the scope captured when the user
starts the action. A nested `json()` scope check does not protect its caller's
next continuation: another already queued Promise reaction can select a new
knowledge base before that continuation runs.

`ConsolePublish.tla` separates build observation, second-mutation dispatch,
publish observation and final UI commit. A concurrent Create operation changes
the scope through the existing refresh/select/clear sequence. The FIFO queue
permits both pending bodies to settle in one ordinary task. Ordinary tasks do
not run while that queue drains.

| Model boundary | Production boundary |
| --- | --- |
| StartPublish | Capture `stamp=currentScope()` and immutable URL prefix. |
| Body / Inner | Response body completion and the scope check within `json()`. |
| Send | Recheck the original stamp before calling the publish POST. |
| Commit | Recheck the original stamp immediately before the success message. |
| RefreshInner / Select / Clear | Concurrent Create's refresh and scope replacement. |
| Reject | Failed build/publish; no success message or automatic retry. |

The positive model checks both the owner at dispatch and the owner at the UI
write. Two independent negative controls remove one guard each; each must
violate its named invariant. The Node contracts execute the actual console
module with controlled Promise resolution and DOM-shaped objects. Equivalent
browser cases run through the existing mandatory console-scope integration
suite. These are complementary evidence, not an automatic refinement proof.

Bounds: one build/publish, one Create, two scopes, revisions 0/1, at most two
queued reactions. The source stamp also contains the token; this model holds
that token constant. Arbitrary token changes, multiple Create operations,
network cancellation, backend authorization, actual publication transactions,
server recovery, browser lifetime and liveness are outside this model. A
publish already sent may have committed after navigation; suppressing its UI
message does not roll back that backend operation.

The original upload and citation models remain unchanged. Their whole-module
source digest is refreshed only after reviewing that their handlers, guards,
cleanup and error ownership were not changed by the publish repair.
