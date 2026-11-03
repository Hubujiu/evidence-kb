# 当前状态机覆盖清单

本清单对应 registry schema 2。全部已发现并登记的所有者都有有限模型；`partial` 表示仍保留边界与精化义务，不表示产品存在已知失败，也不表示全系统正确性证明。每项精确配置、源码映射、假设和未完成义务以[注册表](../formal/registry-v1.json)为准。

| 状态所有者 | 正模型配置 | 错误控制与路径见证 | 源码所有者 |
|---|---:|---:|---|
| physical_budget | 4 | 8 | `kb_platform/rag_testing/budget.py`, `kb_platform/rag_testing/provider_capture.py`, `kb_platform/embeddings.py`, `kb_platform/google_semantic_provider.py`, `kb_platform/model_http.py`, `kb_platform/rag_testing/delivery_http.py`, `kb_platform/rag_testing/reader_diagnostic.py`, `kb_platform/rag_testing/pageindex_probe.py`, `kb_platform/rag_testing/pageindex_full_query.py`, `kb_platform/model_errors.py`, `kb_platform/rag_testing/local_rerank.py`, `kb_platform/delivery.py` |
| budget_batches | 2 | 6 | `kb_platform/rag_testing/budget_batches.py`, `kb_platform/persistence_migration.py` |
| provider_receipts | 2 | 10 | `kb_platform/provider_runtime.py`, `kb_platform/provider_receipt_database.py`, `kb_platform/remote.py`, `kb_platform/database_providers/sqlite_guard.py` |
| turn_budget | 1 | 5 | `kb_platform/agent_model.py` |
| session_commit | 1 | 2 | `kb_platform/harness_sessions.py`, `kb_platform/session_store.py` |
| plugin_lifecycle | 13 | 86 | `rust/plugin-runtime/src/protocol.rs`, `rust/plugin-runtime/src/coordinator.rs` |
| host_process | 3 | 5 | `rust/plugin-host/src/lib.rs`, `rust/plugin-host/src/process_plugin.rs`, `rust/plugin-host/src/process_tree.rs`, `rust/plugin-host/src/process_tree/unix.rs`, `kb_platform/host_protocol.py`, `kb_platform/host_worker.py`, `kb_platform/plugin_process.py` |
| llm_registry | 2 | 2 | `rust/llm-service/src/registry.rs`, `rust/llm-service/src/executor.rs`, `kb_platform/model_service.py` |
| llm_stream | 1 | 2 | `rust/llm-service/src/assembler.rs`, `rust/llm-service/src/service.rs` |
| kernel_execution | 1 | 3 | `rust/agent-kernel/src/lib.rs`, `kb_platform/agent_native.py` |
| loop_graph | 2 | 6 | `rust/agent-services/src/agent_loop.rs`, `rust/agent-services/src/graph.rs`, `kb_platform/harness_loop.py` |
| knowledge_workflow | 1 | 3 | `kb_platform/knowledge_workflow.py`, `kb_platform/knowledge_turn.py`, `kb_platform/knowledge_state.py`, `kb_platform/agent_builtin_tools.py`, `kb_platform/agent_work_state.py`, `kb_platform/knowledge_answer.py`, `kb_platform/rag_testing/context_decisions.py`, `kb_platform/knowledge_planning.py`, `kb_platform/harness_graph.py`, `rust/agent-services/src/graph.rs` |
| storage_registry | 2 | 7 | `kb_platform/storage_service.py`, `kb_platform/database_service.py`, `kb_platform/blob_service.py` |
| storage_transaction | 1 | 1 | `kb_platform/storage_structured_wire.py`, `rust/storage-service/src/transaction.rs`, `kb_platform/database_providers/base.py` |
| storage_value_call | 1 | 1 | `rust/storage-service/src/service.rs`, `kb_platform/storage.py`, `kb_platform/storage_adapters/values.py`, `kb_platform/storage_protocol.py`, `kb_platform/storage_service.py`, `rust/storage-service/src/operations.rs` |
| transfer_protocol | 1 | 1 | `kb_platform/contract_transfer.py`, `kb_platform/storage_object_wire.py` |
| auth_session | 1 | 1 | `kb_platform/authentication.py` |
| authority_publication | 1 | 2 | `kb_platform/core.py`, `kb_platform/agent_tools.py`, `kb_platform/knowledge_service.py`, `kb_platform/semantic_pages_v2.py`, `kb_platform/knowledge_semantic_contract.py`, `kb_platform/rag_testing/publication_embedding_contracts.py` |
| search_generation | 1 | 3 | `kb_platform/search_router.py`, `kb_platform/embedded_search.py`, `kb_platform/semantic_pages_v2.py`, `kb_platform/search_evidence.py`, `kb_platform/rag_testing/search_router_contracts.py`, `kb_platform/rag_testing/publication_embedding_contracts.py` |
| embedding_jobs | 2 | 17 | `kb_platform/embedding_jobs.py`, `kb_platform/knowledge_sql.py`, `kb_platform/knowledge_storage.py`, `kb_platform/rag_testing/publication_embedding_contracts.py`, `kb_platform/rag_testing/embedding_lease_contracts.py`, `kb_platform/storage_adapters/structured.py` |
| provider_admission | 4 | 11 | `kb_platform/provider_admission.py`, `kb_platform/delivery.py`, `kb_platform/provider_runtime.py`, `kb_platform/remote.py`, `kb_platform/model_errors.py`, `kb_platform/model_http.py`, `kb_platform/rag_testing/provider_capture.py` |
| circuit_tool_retry | 1 | 7 | `kb_platform/provider_runtime.py`, `kb_platform/remote.py`, `kb_platform/knowledge_planning.py` |
| cache_evidence | 5 | 17 | `kb_platform/rag_testing/cache.py`, `kb_platform/knowledge_contracts.py` |
| ingestion_migration | 3 | 11 | `kb_platform/documents.py`, `kb_platform/core.py`, `kb_platform/knowledge_transfer.py`, `kb_platform/storage_reset.py`, `kb_platform/storage_adapters/structured_transfer.py`, `kb_platform/database_migrations.py` |
| acceptance_workflow | 1 | 2 | `kb_platform/rag_testing/delivery_http.py` |
| provider_circuit | 3 | 1 | `kb_platform/provider_runtime.py`, `kb_platform/remote.py` |
| knowledge_turn_postflight | 1 | 4 | `kb_platform/knowledge_turn.py`, `kb_platform/agent_tools.py` |
| console_citation_view | 1 | 4 | `kb_platform/console.mjs`, `kb_platform/console.html` |
| agent_console_draft | 1 | 3 | `kb_platform/agent_console.mjs`, `kb_platform/agent_console.html` |
| console_upload_commit | 1 | 1 | `kb_platform/console.mjs`, `kb_platform/console.html` |
| tool_executor | 1 | 4 | `kb_platform/provider_runtime.py`, `kb_platform/knowledge_planning.py`, `kb_platform/knowledge_contracts.py`, `kb_platform/knowledge_platform_contract.py` |
| session_catalog | 1 | 8 | `kb_platform/session_store.py`, `kb_platform/session_storage/schema.py` |
| session_durable_adapter | 1 | 13 | `kb_platform/harness_sessions.py`, `kb_platform/session_store.py` |
| object_transfer_ownership | 2 | 4 | `kb_platform/storage_object_wire.py` |
| remote_retry_ownership | 4 | 16 | `kb_platform/remote.py`, `kb_platform/luna_sdk.py`, `kb_platform/provider_runtime.py` |
| page_recovery | 3 | 9 | `kb_platform/ocr.py`, `kb_platform/page_parsing.py`, `kb_platform/parsing_backends.py`, `kb_platform/documents.py` |
| storage_handles | 4 | 19 | `kb_platform/storage_structured_wire.py` |
| storage_schema_admission | 4 | 29 | `kb_platform/storage_structured_wire.py`, `kb_platform/storage_adapters/structured.py`, `kb_platform/database_service.py`, `kb_platform/storage_sdk/relational.py`, `kb_platform/database_migrations.py` |
| acceptance_binding_publication | 1 | 4 | `kb_platform/rag_testing/delivery_http.py` |
| acceptance_client_execution | 1 | 6 | `kb_platform/rag_testing/delivery_http.py`, `kb_platform/rag_testing/runner.py` |
| platform_upload_snapshots | 2 | 19 | `kb_platform/knowledge_platform_contract.py`, `kb_platform/platform_transfers.py`, `kb_platform/rag_testing/platform_transfer_owner_contracts.py`, `kb_platform/rag_testing/knowledge_platform_contracts.py`, `scripts/rpc_transfer_owner_checks.py`, `kb_platform/rag_testing/rpc_transfer_owner_lint_contracts.py` |
| auxiliary_ocr_upload | 1 | 8 | `kb_platform/knowledge_model_ports.py`, `kb_platform/rag_testing/auxiliary_ocr_transfer_contracts.py`, `kb_platform/knowledge_capabilities.py`, `kb_platform/plugin_host.py`, `scripts/rpc_transfer_owner_checks.py`, `kb_platform/rag_testing/rpc_transfer_owner_lint_contracts.py` |
| sdk_gateway | 3 | 22 | `kb_platform/rag_testing/sdk_gateway.py`, `kb_platform/rag_testing/pageindex_full_index.py`, `kb_platform/rag_testing/pageindex_full_query.py` |
| llm_multiblock | 1 | 8 | `rust/llm-service/src/assembler.rs` |
| canonical_page_publication | 2 | 12 | `kb_platform/semantic_page_ingestion.py`, `kb_platform/google_semantic_provider.py` |
| python_host_lifecycles | 4 | 20 | `kb_platform/plugin_host.py`, `kb_platform/host_applications.py`, `kb_platform/host_worker.py` |
| persistent_migration_journal | 2 | 14 | `kb_platform/database_migrations.py` |
| persistent_document_revisions | 1 | 9 | `kb_platform/storage_adapters/values.py` |
| native_storage_sessions | 2 | 8 | `rust/storage-service/src/service.rs` |
| check_process_owner | 1 | 13 | `scripts/check_process_owner.py`, `scripts/check_process_owner_linux.py` |
| knowledge_atomic_commit | 1 | 4 | `kb_platform/knowledge_sessions.py`, `kb_platform/knowledge_commit_transfer.py` |
| memory_session | 1 | 4 | `kb_platform/harness_sessions.py` |
| delivery_control | 1 | 9 | `scripts/delivery_control.py` |
| application_storage | 1 | 3 | `kb_platform/application_storage.py` |
| redis_cache_provider | 1 | 2 | `kb_platform/storage_adapters/redis_cache.py` |
| formal_tool_files | 1 | 3 | `kb_platform/rag_testing/formal_state_machines.py` |

自动发现还不能覆盖任意隐式状态、动态代码和别名写入。此次扩展生命周期字段和 scripts 扫描，并逐项审阅新增候选；明确的观测、CLI 报告和夹具排除仍记录在注册表，不算生产状态机。
