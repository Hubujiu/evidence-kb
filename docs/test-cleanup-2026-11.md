# 测试去重记录

本轮删除重复执行的合同测试，保留原有权限、预算和请求身份验证。

## 删除与保留映射

删除位置为 `kb_platform/rag_testing/model_embedding_review_contracts.py` 的 `ModelEmbeddingReviewContracts`。
保留位置为 `kb_platform/rag_testing/model_review_contracts.py` 的 `ModelReviewContracts`。
以下六个方法在两个位置的完整 AST 相同。两个模块的 `ModelFixtures` 类 AST 也完全相同。

| 删除的方法 | 保留的方法 | 保留的行为 |
| --- | --- | --- |
| `test_request_data_copy_preserves_authorization_capability_identity` | 同名方法 | 复制请求隔离数据，同时保留授权对象身份。 |
| `test_observed_pre_send_revocation_uses_original_bound_and_callable_authorizers` | 同名方法 | 发送前撤权阻止请求，并释放预算预留。 |
| `test_known_unsent_transport_releases_physical_and_upper_turn_budgets` | 同名方法 | 已知未发送错误释放物理与回合预算，后续调用仍可成功。 |
| `test_observed_documents_keep_raw_identity_audit_authorization_and_manifest` | 同名方法 | 文档嵌入保留请求身份、审计、授权与 manifest。 |
| `test_observed_query_keeps_explicit_request_context` | 同名方法 | 查询嵌入保留显式请求上下文。 |
| `test_plain_google_texts_only_adapter_stays_supported` | 同名方法 | 普通文本适配器的查询与文档嵌入仍可执行。 |

`ModelEmbeddingReviewContracts` 仍保留恢复物理请求状态和已知未发送错误分类的独立测试。其 PostgreSQL 集成测试未删除。

## 历史功能审计

`blob_migration.py`、`knowledge_transfer.py`、`session_transfer.py` 和历史 RAG 回放入口执行显式迁移或审计。它们不在普通运行时静默切换实现。本轮保留这些入口及其原文、权限、费用和未知请求状态合同。

## 验证

定向命令为 `uv run --no-sync python -m unittest kb_platform.rag_testing.model_embedding_review_contracts kb_platform.rag_testing.model_review_contracts`。
首次执行遇到 native 产物和 Python 生命周期协议不一致，报错 `lifecycle commands use runtime_* action protocol`。该结果不能作为通过结论。需要重建 native 产物后通过统一检查入口复验。

没有调用模型、Embedding 服务或语义裁判。测试使用本地 HTTP 固定响应。

PostgreSQL 搜索删除了未初始化实例的 `database` 与 `schema` 回退。构造函数始终设置这两个字段。合同夹具改为调用真实构造函数并替换连接池，不再通过 `__new__` 绕过初始化。现役 DSN 与数据库绑定能力均保留。

统一入口复验已通过，产物位于 `.local/checks/cleanup-final-20261103/summary.json`。本轮发现并执行 1,045 个 Python 合同，六个删除方法对应的保留测试均在完整发现集合内。聊天与工作台浏览器验收也通过，分别执行 11 和 9 个确定性断言。数据库和实际存储提供商验收因未配置隔离资源而未在本地执行。

最终源码的默认全量检查再次通过，报告为 `.local/checks/cleanup-verified-20261103/summary.json`。该轮包含 examples 的 Python lint 与注释检查，全部合同发现完整，未跳过测试或降低通过门槛。
