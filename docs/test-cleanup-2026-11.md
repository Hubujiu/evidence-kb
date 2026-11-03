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

## 初始化边界

PostgreSQL 搜索删除未初始化实例的 database 与 schema 回退。构造函数始终设置这两个字段。合同夹具调用真实构造函数并替换连接池，不再通过 __new__ 绕过初始化。现役 DSN 与数据库绑定能力均保留。

## 验证范围

本次后端 PR 保留原界面，不包含未提交的 Web 页面、静态资源路由及其浏览器测试。源码注释解析依赖安装在已有 e2e 工具目录，不依赖 web 目录。

PR #32 新增的 Rust 注释也移除。形式化 registry 仅刷新已审查的注释删除对应 Rust 文件指纹，模型、负对照和 Python 绑定保持原样。哈希一致不等于源码精化证明。

使用普通完整 CLI 和附加 formal-only CLI 分别验证。真实数据库与存储提供商验收由原 CI 资源运行，本地合成合同不能替代这些验收。没有新增模型、Embedding 或语义裁判调用。
