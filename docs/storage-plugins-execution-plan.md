# 公共存储插件执行与验收

本轮按[六类存储合同审批稿](storage-contracts-v2-approval-plan.md)实施。接口、适配器和消费者已迁入新的公共存储入口；最终统一检查、独立复核和合并结果以本轮验收记录为准。结构见 [存储插件架构](storage-plugins-design.md)，实现清单见 [插件目录](plugin-catalog.md)。

前序 SQL 与对象基础改造的 622 项合同和 10 组真实存储验收属于旧基线。它们不能替代六类合同、Redis、独立 document source 和新进程桥的本轮验收。旧报告保留只读，本轮使用新的输出目录。

## 阶段与退出条件

| 阶段 | 当前交付 | 验收要求 |
| --- | --- | --- |
| P0 合同和基线 | 六类接口、必需扩展、后端边界、批准范围 | 冻结用例与配置，旧结果和本轮结果分别记录 |
| P1 统一绑定 | SDK、具名实例和 export、类型化槽位、注册授权 | 合同缺失、版本不符、未声明槽位、错误身份均拒绝 |
| P2 关系和对象迁移 | 三种 SQL、files、S3，关系消费者与 object 槽位 | 事务、会话、六向 clone、对象完整性与租约保持 |
| P3 文档与 KV | PG 与 SQLite document、三库 durable KV、Redis expiring KV、可丢失 StageCache payload | revision 条件写入、删除后重建、文档搬迁、TTL、缓存丢失重建与合同拒绝 |
| P4 检索拆分 | 独立 lexical、vector、document source，三方身份与封存检查 | 同实例、不同数据库、不同提供商组合；故障和损坏不发布 |
| P5 配置和进程调用 | v2 配置、显式初始化与转换、有界进程桥、对象搬迁 | 非知识库插件可直接复用；旧公共注册入口无活动调用；无运行时格式回退 |
| P6 最终验收与合并 | 本轮统一 checker、真实数据库、HTTP 和浏览器验证 | 固定分母全部报告，完成独立复核，清理专属资源后提交合并 |

本轮本地最终结果和清理见[验收记录](storage-contracts-v2-acceptance.md)，远端合并见 [PR #25](https://github.com/Hubujiu/evidence-kb/pull/25)。实际 PASS、FAIL、ERROR 和未执行项由本轮报告给出，不复用旧 PASS，不因缺少资源跳过必需项。

## 配置目标绑定

1. 选择稳定 `application_id`、owner 与 space。开发、验收和生产分别使用自己的身份。
2. 复制 [v2 初始化模板](../config/storage.example.json)。配置实例的驱动参数与凭据环境变量，再选择每个服务的 export。
3. 让 profile 的消费者实例与 `bindings` 对应。示例要求 `repository`、`originals`、`store`，将 `search` 设为 `platform.search.storage`，并增加 `{"id":"cache","use":"storage.cache.volatile"}`。不使用独立缓存插件时同时删除 `cache` 的绑定和未使用的实例、服务。
4. 确认消费者声明的槽位与配置完全一致。知识库仓储、会话和缓存索引需要 structured 合同，预算额外需要 guarded 扩展；不再要求数据库厂商。
5. 首次创建空间时，运行显式初始化命令生成含 `dataset_id` 的新文件。缓存绑定已有 `cache_id`，不创建持久 dataset。
6. 将 `KB_STORAGE_CONFIG` 指向初始化后的文件。正常启动只打开和核对现有身份，不能直接使用缺少 `dataset_id` 的初始化模板。

```bash
uv run --no-sync python -m kb_platform.storage_configuration initialize --input config/storage.example.json --output .local/storage-configured.json
export KB_STORAGE_CONFIG="$(pwd)/.local/storage-configured.json"
```

初始化命令不覆盖输入或已有输出。对已有数据集，先在输入绑定中填入核验后的 `dataset_id`；命令只验证这个身份，不因目标为空或身份不符重新初始化。不要删除身份字段来绕过旧数据核验。

示例让 repository 和 BM25 使用同一 PostgreSQL 实例，dense 使用另一 PostgreSQL，source 使用 SQLite，会话使用 MySQL，原件使用 files，StageCache payload 使用 Redis。source 是独立检索原文适配。领域仓储可绑定 PostgreSQL、MySQL、SQLite 的 structured 出口；各逻辑空间独立。PG lexical 需要 pg_textsearch，vector 需要 pgvector。会话绑定须在应用 profile 中显式选用 `harness.store.database`，缓存须显式选用对应缓存插件；删除未使用的消费者绑定。

把 dense 服务改为 embedded 时，实例配置为 `{"provider":"embedded","config":{"root":".local/storage/dense"}}`，服务 export 保持 `vector`。迁移旧索引和初始化新目标须独立完成，修改配置本身不复制数据。files 切换 S3 同样需要显式对象复制和业务引用切换。

旧 v1 数据库配置只能离线转换：

```bash
uv run --no-sync python -m kb_platform.storage_configuration convert-databases --input .local/databases-v1.json --output .local/storage-v2-template.json
```

转换器只转换原有关系绑定和已知身份，不搬迁数据，也不替旧绑定猜测 `dataset_id`。核对输出后再显式初始化或验证已有空间。运行时拒绝 `KB_DATABASES_CONFIG` 和旧格式，不自动回退。

独立 core 应用使用同一 `KB_STORAGE_CONFIG`，或向 `create_agent_application` 传入 `storage_configuration`。任意应用可调用 `compile_storage_plugins` 并传入自己的 provider catalog。业务插件用 `PluginSpec.requires` 中的类型化存储需求 声明需求，mount 中调用 `require_storage`。第三方也可以直接依赖自己的服务；公共服务不强制接管所有插件。

## 运行统一检查

修改活动源码后，从项目根目录运行：

```bash
uv run --no-sync python scripts/check_project.py
```

新环境先执行 `uv sync --locked --group dev`。必要合同在 `kb_platform/rag_testing/*_contracts.py`，每个模块必须能发现非零用例。不能另建评分器、忽略退出码、修改门槛或跳过失败。

真实服务验收使用同一入口：

```bash
uv run --no-sync python scripts/check_project.py --local-integration --storage-integration --output reports/storage-check-new
```

`--output` 必须是不存在的新目录。真实存储模式需要以下环境变量：

| 环境变量 | 用途 |
| --- | --- |
| `KB_STORAGE_TEST_POSTGRES_URL` | 主知识库与 BM25 的独立 PostgreSQL 验收库 |
| `KB_STORAGE_TEST_DENSE_POSTGRES_URL` | 与主库不同的 dense PostgreSQL 验收库 |
| `KB_STORAGE_TEST_MYSQL_URL` | 独立 MySQL 验收库 |
| `KB_STORAGE_TEST_REDIS_URL` | 专用 Redis 缓存 |
| `KB_STORAGE_TEST_S3_ENDPOINT` | 本地 S3 兼容服务 |
| `KB_STORAGE_TEST_S3_ACCESS_KEY`、`KB_STORAGE_TEST_S3_SECRET_KEY` | S3 验收服务凭据 |
| `KB_INTEGRATION_DSN` | 原有 PostgreSQL HTTP 与解析集成的专用验收库 |

SQL URL 必须使用 `127.0.0.1`、显式端口和 `rust_plugin_acceptance_` 前缀库名。两个 PG 目标必须不同。S3 endpoint 必须是 loopback HTTP；Redis 使用专用 loopback 实例、显式端口和 `/0`。凭据不写入公开报告。真实服务不可用时验收失败，不以 mock 或 SQLite 冒充该服务。

最终 checker 构建当前 Rust 宿主。总结果位于 `summary.json`，存储观察位于 `storage-integration/report.json`。原有浏览器入口使用同一 checker 制品：

```bash
uv run --no-sync python -m kb_platform.rag_testing.browser_integration --checker-summary reports/storage-check-new/summary.json --output reports/storage-browser-new
```

测试前枚举本轮计划用例，报告保留固定分母、失败和未执行项。离线合同、真实存储组件、合成 HTTP、浏览器分别统计，不能把它们相加成 RAG 答案正确率。

## 核对关键行为

| 场景 | 必须观察到的结果 |
| --- | --- |
| 同一提供商多 export | 按声明取得所需接口，未声明的方法不可调用 |
| 同类多个实例 | 显式选择生效，错误数据身份或误指空库拒绝 |
| 多应用与多插件 | 相同逻辑表名不串数据，空间 owner 与企业 ACL 分别核验 |
| 替换或关闭实例 | 旧句柄拒绝，在途事务先结束，连接只由所有者关闭 |
| 并发迁移和运行权限 | 迁移锁、内容 hash 和 dirty 生效，运行账号拒绝直接 DDL |
| 三库会话 | revision、事件顺序、幂等完成与单写者租约一致 |
| 文档 CAS 与迁移 | 删除后重建不会复用旧 revision，过期条件写入拒绝；快照身份与删除历史保留 |
| Redis 缓存 | TTL 生效，丢失或断线是未命中，恢复后可重建；持久槽位拒绝 Redis |
| 分库检索 | 五种 PG 与 embedded 组合均真实执行，单侧故障或 source 损坏不发布半成品 |
| 权限撤销 | 查询过滤先于截断，证据取回与最终交付复查当前 ACL |
| files 与 S3 | 长度、SHA-256、租户和身份核验，双向复制返回明确引用映射 |
| 进程存储 | 方法、参数和结果有界，拒绝前不写入，已完成写入的结果异常有明确状态 |
| 缓存与费用 | 精确依赖不变，失败、缺失 usage、UNKNOWN 与历史费用不丢失 |

本轮使用合成文档、本地模型夹具和固定向量。外部模型、Embedding 和 judge 新增调用为 0；不声称生产 RAG 质量或数据库性能提升。若以后增加付费调用，必须先核对现有授权、预算和调用上限。

## 显式搬迁数据

1. 冻结旧写入口，等待在途事务结束，记录无法确认的物理请求。
2. 创建一致性备份和不可变导出，保存结构版本、数据身份和内容哈希。
3. 导入离线目标，核对全部记录、权限、revision、引用、租约和费用。
4. 运行当前合同，确认目标数据身份，再切换配置并执行读写冒烟。
5. 保持旧端写保护。新端已有写入时，回退须显式处理新增数据，不能同时启用两端写入。

会话使用 `session_transfer.export_sessions` 和 `import_sessions`。它们核对记录哈希、事件序号、revision 和响应，生成新的目标 `dataset_id` 与源目标映射。旧本地会话可只读导出再导入新目录：

```bash
uv run --no-sync python -m kb_platform.session_transfer export --legacy-sqlite .local/old-agent/sessions.sqlite3 --source-frozen --output reports/sessions-export-new.json
uv run --no-sync python -m kb_platform.session_transfer import --input reports/sessions-export-new.json --root .local/new-agent --target-offline
```

文档使用 `storage_adapters.values.export_documents` 与 `import_documents`，明确源目标数据身份。快照保存文档及删除历史，输入完整校验后才导入，普通运行不回读旧格式。对象使用 `storage_object_transfer.transfer_objects` 在 files 与 S3 间复制，完整验证后返回引用映射，不自动修改数据库引用。旧原件路径可用 `scripts/migrate_original_blobs.py` 迁移。

预算使用 `persistence_migration.inspect_ledger_import` 和 `import_ledger`，核对冻结 SQLite 的文件哈希、逻辑快照、caps、全部物理请求和 UNKNOWN。保持 `BudgetLedger` 唯一实现。前序账本、`producer_guard`、模型 capture 和保留的文件入口见[本地持久化边界](storage-plugins-design.md#保留的本地持久化边界)。

## 清理与交付

测试只创建本轮唯一标识的容器、库、空间、桶和目录。`kb_platform.rag_testing.storage_resources` 的 prepare 记录容器归属、镜像身份和私有环境；cleanup 校验精确名称和所有权标签后删除本轮容器及其卷，再删除私有凭据。共享镜像缓存、已有服务和其他运行的数据保持原状。

保留计划、trace、实际请求观察、报告和清理记录。清理失败须记录具体残留。最终提交记录源码、文档和可重复入口；合并前检查当前分支、必需门禁、独立复核及远端合并条件。
