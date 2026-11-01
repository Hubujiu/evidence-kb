# 检索存储与恢复

当前平台通过 `platform.search.storage` 分别绑定 lexical BM25、vector、document 三个合同。正文、ACL、引用、构建和 Embedding 任务通过 structured 关系合同保存；原件通过 object 合同下载。提供商配置见 [公共存储架构](storage-plugins-design.md)。

## 独立检索与发布

BM25 与 dense 可以使用同一 PostgreSQL、不同 PostgreSQL 数据库，或 PostgreSQL 与 embedded。通用 PostgreSQL lexical 只要求 pg_textsearch，vector 只要求 pgvector。镜像固定 PostgreSQL 18.6、pg_textsearch 1.5.1、pgvector 0.8.6，构建校验见 `docker/postgres/Dockerfile`。当前向量检索使用授权子集上的精确余弦距离，没有启用 HNSW 或量化。

检索组合从独立 document source 读回权威原文，先按当前权限过滤，再执行 TopN。候选 ID、原文哈希、三方 dataset 与封存内容必须一致。原件只供下载与预览；不可变解析快照用于证据，原件缺失不影响已保存的证据文本。

构建依次提交 STAGING 与引用，在事务外执行 Embedding，写入各搜索通道，读回并封存后进入 VALIDATED，最后通过短事务切换发布指针。某通道失败、权限变更或封存不全均不能发布。崩溃可以留下未发布代际，显式恢复仍核对实现、模型、权限、源版本、单元和引用；变化则拒绝旧构建。

## Embedding 任务恢复

`embedding_jobs` 保存逐批输入依赖和结果，`embedding_attempts` 保存尝试关联，`embedding_circuits` 保存熔断状态。统一 BudgetLedger 继续记录每个物理请求的预留、费用与 UNKNOWN。

- COMPLETE 批次只有在哈希和全部依赖一致时才能复用。
- 暂时性 HTTP 错误遵守有限重试与 Retry-After，等待不占用业务事务。
- RUNNING 超出租约转为 UNKNOWN，旧执行者不得完成新租约的工作。只有匹配的 COMPLETE 回执和已结算账本才能恢复结果。
- 明确未发送的预算不足可在解除条件后显式 resume，不消耗远程失败重试次数。未知物理请求不自动重发。

状态接口为 `GET /knowledge-bases/{kb}/builds/{build}`，恢复接口为 `POST /knowledge-bases/{kb}/builds/{build}/resume`。没有后台隐式付费重试；供应商不支持幂等时不承诺远端 exactly-once。

## 新部署与旧数据

`scripts/bootstrap.py` 显式创建本地 SQLite、embedded、files 的新数据集。可选 Compose 只安装开发账号与 PostgreSQL 扩展，不安装知识库表。其他提供商通过 v2 配置显式初始化，正常业务启动只验证既有身份。

旧 `KB_DSN` 和 `KB_SEARCH_BACKEND` 已删除，不能靠切换环境变量搬迁数据。旧数据库迁移、会话与账本导入见 [存储能力清理与迁移](storage-capability-cleanup.md)。新数据集不接纳旧 Migration 1，也不自动修正旧发布指针。

`scripts/migrate_postgres_search.py`、`snapshot_delivery.py` 及旧 `postgres_search.py` 保留为历史 PostgreSQL 格式工具。它们不初始化当前平台，也不证明旧封存可用于新提供商。旧恢复包必须连同对应代码和配置恢复；当前提供商迁移须逐项核验新身份与内容。

## 验证入口

```powershell
uv run --no-sync python scripts/check_project.py
uv run --no-sync python scripts/check_project.py --local-integration --storage-integration --output reports/storage-check-new
```

真实存储入口覆盖三种关系后端、独立 BM25 与 dense、会话搬迁、对象、Redis、构建恢复及 Rust HTTP。浏览器与统一证据评分结果分别保存。测试使用合成文档、固定向量和本地模型夹具，零外部付费调用，不代表生产答案质量。
