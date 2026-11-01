# 公共存储插件执行与验收

核心实现已落地。本文件保留原 P0–P6 的交付范围，并给出当前验证和部署切换步骤。结构说明见 [存储插件设计](storage-plugins-design.md)，实现清单见 [插件目录](plugin-catalog.md)。代码具备换库能力，不代表已有线上数据已经搬迁。

## 各阶段当前状态

| 阶段 | 已实现内容 | 当前边界 |
| --- | --- | --- |
| P0 数据归属 | SpaceKey、dataset_id、schema owner、明确的历史导入入口 | 实际部署仍需盘点活动写入、备份和未决请求 |
| P1 公共数据库 | registry、三种提供商、slot 编译、静态依赖、授权绑定、迁移与有界进程批次 | 任意消费者需声明自己的 schema 和后端要求 |
| P2 会话 | 三后端会话存储、并发与幂等合同、逐记录导出导入 | 导入是显式离线 clone，生成目标 dataset_id |
| P3 知识库 | 公共 PG 绑定、领域查询、局部 RLS 身份、可独立配置的 BM25/dense | 知识库业务仍只支持 PostgreSQL |
| P4 原件 | BlobRef、files/S3、下载预览、旧原件复制和引用映射工具 | 迁移工具不自动修改数据库中的旧引用 |
| P5 缓存与费用 | 三库缓存索引、预算、数据库回执、可注入准入记录 | 冻结历史、producer_guard sidecar、部分模型审计与旧缓存仍保留文件边界 |
| P6 组合验收 | 统一 checker、三库、六向 clone、分库故障恢复、进程数据库回调、重启后新建索引及浏览器回归全部通过 | 采用合成文档与本地模型夹具，不代表生产 RAG 质量 |

2026-11-01 的[最终统一检查](../.local/checks/storage-final-20261101-04/summary.json)全部 12 项门禁通过：622 项离线合同、17 项宿主集成、2 项原有 HTTP 集成、10 组真实存储集成、进程插件集成和 24 个评分器控制。存储集成另含 78 个事务原子性探针、6 个跨库 clone、两套分库 HTTP 配置、6 个统一合成证据用例及 1 项浏览器用例。[原有浏览器回归](../.local/storage-browser-final-20261101-02/report.json)的 2 项用例使用最终 checker 制品另行通过。所有测试无失败或跳过，新增外部模型、Embedding、judge 调用均为 0。

测试自动清理了 38 个数据空间、1 个 S3 bucket 和本地夹具；三个临时容器及专用验收库随后删除，原有 PostgreSQL 容器保留。构建缓存和浏览器临时副本的目录删除被自动安全审查拦截，返回 `blocked by policy`，因此仍保留；具体路径及已完成清理见[清理记录](../.local/storage-implementation-20261101/cleanup.json)。报告、trace 和截图保留，旧轮次只读。

这些场景使用合成数据与本地模型夹具，不能作为企业人工金标或线上模型质量成绩。报告目录属于本地工作产物，不是产品运行模块。完整汇总见[验收记录](../.local/storage-implementation-20261101/acceptance.json)。

## 配置目标绑定

1. 选择应用中的消费者实例及稳定 `application_id`。开发和生产使用不同身份。
2. 复制 [数据库配置示例](../config/databases.example.json)，配置 route 与 consumer slot。将 `KB_DATABASES_CONFIG` 指向新配置。
3. 为 PostgreSQL 和 MySQL 配置运行连接与迁移连接。使用预配模式时，迁移账号必须有实际所需权限；使用 `provision=false` 时先准备空间与受限账号。
4. 需要拆分检索时，设置 `KB_SEARCH_CHANNELS_CONFIG`。配置中的 `search_bm25`、`search_dense` 实例 ID 必须与数据库绑定一致。
5. 需要对象存储时，在 profile 选择 `blobs.service`、`blobs.provider.s3`、`blobs.binding.s3` 和 `platform.originals.blobs`。本地对象后端对应 `files`。
6. 对已有数据设置核验后的 `expected_dataset_id`。先检查配置与身份，再启动消费者。

独立 core 应用同样读取 `KB_DATABASES_CONFIG`，也可向 `create_agent_application` 传入 `database_configuration`。任意应用可直接调用 `compile_database_plugins(profile, catalog, configuration)`。新插件通过 `PluginSpec.storage_slots` 声明 slot，mount 中使用 `require_database`。业务插件发布并执行自己的 `SchemaPackage`，完成 `prepare`、`seal` 后提供业务服务。不要把知识库表结构放进数据库提供商。

不要通过修改数据库 URL 把 PostgreSQL 知识库切成 MySQL。当前会话及通用持久化支持三库，知识库 schema、RLS、任务锁和 PG 搜索仍有明确后端要求。

## 运行统一检查

修改活动源码后，从项目根目录运行：

```powershell
uv run --no-sync python scripts/check_project.py
```

新环境先运行 `uv sync --locked --group dev`。离线合同使用真实 SQLite，不调用外部模型。数据库、会话、原件、持久化及搜索路由合同位于 `kb_platform/rag_testing/*_contracts.py`，统一发现非零用例。不能另建独立评分器、忽略失败或降低门槛。

真实存储验收接入同一个入口：

```powershell
uv run --no-sync python scripts/check_project.py --storage-integration --output reports/storage-check-new
```

`--output` 必须是尚不存在的新目录。该模式要求配置以下环境变量：

| 环境变量 | 用途 |
| --- | --- |
| `KB_STORAGE_TEST_POSTGRES_URL` | 主知识库与 BM25 的独立 PostgreSQL 验收库 |
| `KB_STORAGE_TEST_DENSE_POSTGRES_URL` | 与主库不同的 dense PostgreSQL 验收库 |
| `KB_STORAGE_TEST_MYSQL_URL` | 独立 MySQL 验收库 |
| `KB_STORAGE_TEST_S3_ENDPOINT` | 本地 S3 兼容服务 |
| `KB_STORAGE_TEST_S3_ACCESS_KEY`、`KB_STORAGE_TEST_S3_SECRET_KEY` | 验收服务凭据 |

数据库 URL 必须使用 `127.0.0.1`、显式端口及 `rust_plugin_acceptance_` 前缀库名。两个 PostgreSQL 目标必须不同。S3 endpoint 必须是 loopback HTTP。入口构建当前 Rust 宿主后执行存储集成，真实服务不可用时不能用 SQLite 或 mock 结果替代。

原有 `--local-integration` 和 `KB_INTEGRATION_DSN` 继续用于原本的 HTTP、解析及 PostgreSQL 集成。存储验收的完整观测在 `storage-integration/report.json`，总结果在 checker 输出目录的 `summary.json`。

## 核对行为和故障恢复

验收按固定场景核对结果，不只检查建表成功：

| 场景 | 必须观察到的行为 |
| --- | --- |
| 同一服务器上的独立空间 | 相同逻辑表名不串数据，错误 dataset_id 拒绝 |
| 两个连接竞争同一会话 | revision、事件顺序及单写者租约保持一致 |
| 完成请求重放、未决请求恢复 | 完成响应可核对，未决物理请求不自动重发 |
| route 替换或关闭 | 拒绝旧注册句柄，在途事务先结束再清理连接 |
| 同时迁移、内容哈希冲突 | 数据库锁串行迁移，同版本不同内容拒绝 |
| MySQL DDL 中断 | dirty 可见，消费者不能当作迁移完成 |
| 运行时 DDL | 受限账号或 SQLite authorizer 拒绝 |
| SQLite 替换与 WITH 替换写 | 追加表由数据库触发器保护，批次整体回滚，原记录保持 |
| 身份与迁移元数据 | 广授权被收紧；危险预配账号拒绝；重启前后身份和 hash 不变 |
| PG 连接复用和权限撤销 | 当前 tenant、kb 和权限重新核对 |
| PG 封存后重启再构建 | 旧代际仍拒写，新角色可以新建及封存另一代际 |
| 搜索一侧失败或内容不一致 | 不发布半成品，不用另一侧结果伪造完整性 |
| 文件与 S3 原件 | 校验长度和哈希，越权、缺失和中断有明确结果 |
| 缓存和预算迁移 | 精确依赖与当前授权不变，失败和 UNKNOWN 费用不丢失 |

RAG 验收先盘点冻结计划、trace、实际请求账本、缓存及已有裁判。只改评分直接重评已有 trace；只改存储先执行当前确定性评分和真实存储组件路径。被测存储不能被上层缓存跳过。合成 HTTP 问答与业务生产质量分开报告。

## 显式搬迁数据

1. 冻结旧写入口，等待在途事务结束，记录无法确认的请求。迁移函数中的布尔声明不能替代实际停写。
2. 创建一致性备份与不可变导出，保存结构版本、数据身份和哈希。
3. 导入离线目标，核对每条记录、引用、权限、revision、租约及费用。
4. 运行当前合同，确认目标绑定及 dataset_id。
5. 保持旧端写保护，切换配置并执行读写冒烟。
6. 如需回退，先核对新端已发生的写入，再选择恢复点。不能同时启用两端写入。

### 会话

`session_transfer.export_sessions` 与 `import_sessions` 接受任一已绑定后端，检查表结构、记录哈希、事件序号、revision 和请求响应。SQLite、PostgreSQL、MySQL 六个有向组合已有真实 clone 场景。当前导入产生新的 dataset_id，并保留 source/target 映射；不宣称保留旧数据身份。

旧 SQLite 会话可先只读导出，再导入新本地目录：

```powershell
uv run --no-sync python -m kb_platform.session_transfer export --legacy-sqlite .local/old-agent/sessions.sqlite3 --source-frozen --output reports/sessions-export-new.json
uv run --no-sync python -m kb_platform.session_transfer import --input reports/sessions-export-new.json --root .local/new-agent --target-offline
```

CLI 的 `--root` 是本地入口。远程数据库迁移使用同模块的 Python 接口及已配置 `SessionStore(database=...)`，当前没有可冒用的 `--postgres` 或 `--mysql` CLI 参数。

### 预算账本

`persistence_migration.inspect_ledger_import` 读取冻结 SQLite 的文件哈希和逻辑快照。`import_ledger` 必须接收两者的预期值，核对 caps、全部物理请求、缺失 usage 和 UNKNOWN，记录目标映射并封存旧 producer。旧账本已有导入记录时，重新绑定还要通过 `validate_ledger_binding`。

保持 `BudgetLedger` 唯一实现。冻结 `budget_batches` 及其文件身份不转换成 DSN；专用 SQLite `producer_guard` sidecar 继续负责互斥和封存。模型请求 JSON capture、旧 Luna 缓存和默认文件回执的边界见 [存储设计](storage-plugins-design.md#保留的本地持久化边界)。

### 原件

`scripts/migrate_original_blobs.py` 接受包含 `tenant_id`、`original_path`、`original_sha256` 的 JSON 数组，将原件复制到 files 或 S3，输出引用映射及审计。示例：

```powershell
uv run --no-sync python scripts/migrate_original_blobs.py --records reports/original-records.json --legacy-root .local/old-originals --files-root .local/new-originals --output reports/originals-migration-new
```

工具报告 `database_updated: false`。核验对象完整性后，再由知识库存储路径切换数据库引用。旧版本、manifest、原文 hash 和金标保持只读。
