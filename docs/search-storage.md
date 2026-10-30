# PostgreSQL 检索存储与恢复

默认 `KB_SEARCH_BACKEND=postgres`。PostgreSQL 18.6 + pg_textsearch 1.5.1 + pgvector 0.8.6 保存正文、页面、向量、引用、ACL、构建状态和发布清单。镜像源和二进制校验和固定在 `docker/postgres/Dockerfile`。没有启用 HNSW、量化或模型替换。当前检索走授权子集上的精确余弦距离。

## 数据与事务

每个构建有独立数据库表，正文与页面使用不同 BM25 统计。复用既有中文分词；token 编码为固定 ASCII 标识，防止数据库再次分词改变边界。各字段分别取 TopN，再按既有最大字段加权分数合并。授权过滤先于 LIMIT；绑定包含原文版本和解析版本。

构建数据与 BM25 索引在同一 PostgreSQL 事务内创建。完成后应用角色失去 INSERT 权限，不授予 UPDATE/DELETE。解析正文和引用是权威数据；原件只供备份下载，原件缺失不会阻断证据预览和上下文读取。

构建分阶段执行：创建 STAGING 和引用；事务外执行外部 Embedding；提交搜索代际；核验后 VALIDATED；最后在短事务内切换发布指针。崩溃可能留下未发布代际，但不会让用户读到半个新版本。恢复会校验冻结实现、权限、源版本、检索单元与已提交引用，变更则拒绝旧构建。语义页的活动 manifest 和校验和也保存在数据库；本地 manifest 文件只是审计副本。

## Embedding 任务恢复

`embedding_jobs` 保存逐批输入依赖和结果，`embedding_attempts` 保存尝试关联，`embedding_circuits` 保存熔断状态。每次物理请求有新身份；费用继续由现有统一 BudgetLedger 记录，不新增费用账本。

- COMPLETE 批次校验哈希后复用；同一构建的输入、模型和授权依赖改变时拒绝恢复。
- 明确的暂时性 HTTP 错误最多 3 次，采用退避与 Retry-After；返回 `retry_at`，不占用数据库事务等待网络。
- 连续故障打开持久化熔断。状态接口和恢复接口分别为 `GET /knowledge-bases/{kb}/builds/{build}` 与 `POST /knowledge-bases/{kb}/builds/{build}/resume`。
- RUNNING 超出租约转 UNKNOWN；旧执行者被隔离。只根据匹配的 COMPLETE 提供者回执及已结算账本恢复，无法判定的请求不自动重发。
- 确认尚未发送的预算不足和未决账本阻塞保留为可恢复状态；解除条件后显式 resume，不消耗远程失败的重试次数。输入超限仍是终止错误。
- 当前恢复由显式 resume 请求驱动；服务没有隐式后台付费重试。供应商不支持幂等键时，不承诺远端 exactly-once 或绝无重复计费。

## 安装与升级

新环境运行 `docker compose up -d --build --wait postgres`。初始化依次应用 `sql/001_init.sql`、`002_search.sql`、`003_embedding_jobs.sql`。已有数据库不会自动执行 initdb 文件，必须在备份后显式安装新的迁移 SQL。普通业务使用 `kb_app`，不使用管理员凭据。

本地正式切换使用独立 `metadata_postgres` 卷。旧 `metadata` 卷及本地索引保留。`scripts/migrate_postgres_search.py` 只导入指定租户当前发布版本，校验原文件清单、权威身份、完整单元和原始向量精确回读，不调用 Embedding、不切换发布指针。缺失或损坏明确失败。

语义页使用 `scripts/rebuild_postgres_semantic.py` 从已验证 capsule 重建。只复用与当前 embedding manifest、原文版本和逐页文本完全一致的 capsule。

这次查询修复变更了 PostgreSQL 后端源码指纹。升级后，旧版 STAGING 构建可通过仅匹配已审查源码哈希的兼容检查续跑；其他检索、分块、模型及权限依赖仍须完全一致。已有语义页 manifest 绑定旧后端源码，升级后需使用经过验证的原有 capsule 显式重建 manifest；该步骤不需要重新请求 Embedding。升级前保留数据库备份和 capsule，禁止直接改写旧 manifest 或跳过依赖校验。

## 备份、恢复与容灾边界

`scripts/snapshot_delivery.py create` 默认完整导出 PostgreSQL，包括所有搜索表、向量、manifest 和 Embedding 任务。`verify` 在新数据库还原，核对全部表指纹和已选发布索引完整内容。应用会话、预算账本和身份配置仍按现有恢复包保存；这些应用状态目前仍使用 SQLite/文件，本次统一的是检索与构建数据，不能把整个应用称为无状态分布式系统。

`python -m kb_platform.rag_testing.postgres_recovery --output reports/<新目录>` 在独立 Docker 资源中验证事务回滚、连接中断、SIGKILL、备份还原、物理复制与主库隔离后的手动提升。需要两台节点同版本插件。本地交付运行单 PostgreSQL 实例，没有部署自动故障切换，也不声称跨主机 RPO=0。实际主备部署应配置同步策略、隔离旧主、监控、WAL 保留与独立备份，并在目标环境重做演练。

原始向量 JSON 为精确迁移回读保留，pgvector 列用于检索；BM25 索引和 WAL 另占磁盘。这些是同一数据库管理的副本，不是三个独立系统。

## 验证入口

- `uv run --no-sync python scripts/check_project.py`：离线统一门禁。
- `python -m kb_platform.rag_testing.embedded_operations --backend postgres --output reports/<新目录>`：真实权限、发布和备份缺失行为，需 `KB_DSN`。
- `python -m kb_platform.rag_testing.postgres_operations --output reports/<新目录>`：真实任务中断、恢复、租约、熔断；提供者是受控替身，零远程调用。
- `python -m kb_platform.rag_testing.embedded_acceptance --postgres --suite <冻结suite> --catalog <原始catalog> --delivery-window --output reports/<新目录>`：真实 HTTP 检索链路配对，统一 scorer；模型为替身，不代表真实生成正确率。

回退必须把代码配置与对应的原数据库和索引一起恢复。切换后的新构建没有写回旧后端，不能只修改环境变量就认为完成回退。
