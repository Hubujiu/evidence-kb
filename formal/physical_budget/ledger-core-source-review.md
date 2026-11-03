# Reviewed source bindings for PhysicalLedgerCore and LocalRanker

All 22 existing physical-budget bindings and every other machine record remain unchanged.
All 10 pre-existing physical-budget TLA/CFG files and their prior run definitions remain byte-identical.
Core BudgetLedger production source is unchanged. The only production delta is LocalRanker success finish moved to try/else.

New independently reviewed bindings:

| Path | Symbol | AST SHA256 |
| --- | --- | --- |
| `kb_platform/rag_testing/budget.py` | `validate_caps` | `d9a7ccad4b9c1f4098e3323187ab5301f2fdb00a713c5d6919219b7e23d8c578` |
| `kb_platform/rag_testing/budget.py` | `validate_resources` | `89fee77d475adb8ea3d27cfb4ea677b942166718e7c4f79aeaf4ddef5b5bf8ba` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger.__init__` | `2a37a6384199f9e47a6a8290fbea2806136128abbef2093b259e7a6681d59931` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger._db` | `ce2daef7616c75904ad81fdd6780defc09f7a24f0b2fb96ac5590b2c9089f5df` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger.admission` | `61e62fe32bdef9c9eb57ed76e82d8c30bfe6eca7bfecab4cc8602cae14c8e109` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger._remote_guard` | `2d6aa6781adb5e75f294f34b4fc2f1a10c38724df7f473eda12d571d34a41335` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger._valid` | `59c2eb1fcaae752aed53816830d95a3b919ab39522a61e3b6e7a062b5c574b08` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger._totals` | `7779b3faf5513b346d311c430020b770c2168c2d3f35c73d8eae4bf99914c1d3` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger._sum_rows` | `0227c5abe6d4366a7716768100b67b1d16dc87d070c444c75ceb860b6fe22db1` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger.reserve` | `9ae8aa0d16fb92d784846d2661c3a4a04d7cde9be436a79875f1e2afce49f8f1` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger.summary` | `e86a56273683d919c0cda2361741add269c1b68f784628ab2f30110fd1d21f0c` |
| `kb_platform/rag_testing/budget.py` | `BudgetLedger._records` | `12186d47de323195c30e5740adcca66ffe8dbb7c48f9ec6599f9cb9934cbff31` |
| `kb_platform/rag_testing/local_rerank.py` | `LocalRanker.rank` | `82d9ba122d2c64d006c4952bf94c03ff3bbe392654c33c3c26ac7b13b0ce8239` |
| `kb_platform/rag_testing/physical_ledger_core_contracts.py` | `PhysicalLedgerCoreContracts` | `bd5aa253108da0f02438db532e587533b0ec501f70740c0e62f474c4bd3ffd13` |
| `kb_platform/rag_testing/physical_ledger_core_contracts.py` | `PhysicalLedgerSourceContracts` | `7ed957eb6a7ef19ec1becbc2e139636be79d9c8edba42f6b9add3eeeba041bcc` |
| `kb_platform/rag_testing/local_rerank_contracts.py` | `LocalRerankContracts.test_successful_inference_settlement_failure_is_not_retried_or_masked` | `82745800541e8e36a37ba0c0b6102a6ae7244ec433c900941bb5fd5b2722cb6d` |
| `kb_platform/rag_testing/local_rerank_contracts.py` | `LocalRerankContracts.test_inference_error_remains_cause_of_first_settlement_failure` | `84dc46616cb54a1a97bd512507312c88a219db9bc3ca2bbb3b62582fd9624907` |

The source guard is mandatory even when the AST hash is deliberately refreshed in a mutant fixture.
The registry's generic unknown-cost wording was narrowed to per-coordinate unreported usage: UNKNOWN status may retain an observed partial vector; no existing model property or bound was changed.
