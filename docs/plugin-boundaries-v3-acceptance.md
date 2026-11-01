# 插件边界第三轮验收

本轮从 `ecfd3c3` 开始，保留原业务策略、Rust 生命周期与统一预算实现。静态内置插件由 70 增至 72：新增可绑定 relational 的知识库仓储和共享模型预算资源；按配置生成模型提供商实例，不按查询、表或工作流步骤拆插件。完整目录见 [插件目录](plugin-catalog.md)，结构见 [方案](plugin-boundaries-v3-plan.md)。

## 交付边界

- 知识库业务通过 56 个封闭语义操作访问领域仓储。PostgreSQL、SQLite 共用业务与任务状态机，SQL、锁、JSON 和数据库时钟集中在持久化实现中。
- 通用 PostgreSQL lexical/vector 使用独立搜索 schema，分别只要求 pg_textsearch、pgvector。BM25、dense、证据文档可以绑定不同存储。
- 模型公共服务只持有注册表，每个配置的提供商 endpoint 有独立 Rust 实例、客户端和路由租约；所有端点继续共享现有物理预算账本。挂载时绑定路由的第三方插件声明对应路由依赖。
- Rust 初始化操作可以申请严格绑定本操作、实例与代次的租约；操作完成后不可继续派生调用。真实数据库预算初始化和失败挂载资源释放均纳入合同。

## 真实后端与端到端范围

| 仓储 | BM25 | dense | 证据文档 | 原件 | 验收路径 |
| --- | --- | --- | --- | --- | --- |
| PostgreSQL | PostgreSQL | 另一个 PostgreSQL 数据库 | SQLite | S3 | Rust 主宿主、TCP HTTP、浏览器、重启 |
| PostgreSQL | PostgreSQL | Embedded | SQLite | files | Rust 主宿主、TCP HTTP、重启 |
| SQLite | PostgreSQL | Embedded | SQLite | files | Rust 主宿主、TCP HTTP、浏览器、重启 |

同一领域行为还直接使用 SQLite、PostgreSQL 加 Embedded 检索验收，包含摄取、构建、发布、引用、越权拒绝、撤销后的读取、旧发布指针保持和过期任务 UNKNOWN。SQLite 单独覆盖实际关闭重开、追加只读、并发 authority 更新及 125 条引用构建。

通用检索覆盖五种 BM25/dense 组合、同 PG 空间与两个实际数据库、真实权限撤销故障、源数据损坏及恢复。两个独立临时数据库分别只安装一个搜索扩展，验证通用记录不需要知识库字段或业务表，过滤发生在 top-k 之前，缺失另一种能力时拒绝服务。

模型集成使用真实本地 HTTP 响应，包含独立端点替换、选定路由就绪、第三方消费方的重载范围、关闭排空、部分挂载失败清理、共享预算和 UNKNOWN 重启后阻止重发。它不调用外部模型。

## 复查与失败记录

独立复查实际复现了锁等待后的过期任务仍被完成：业务提前采样的时钟在等待后失效。修复在持久化边界先获得任务行锁，并在原子更新中使用数据库当前时钟；领取租约、重试和熔断截止时间也在数据库内按策略时长计算。永久真实回归同时验证过期完成被拒绝，以及等待熔断锁后获得新租约。

原始失败产物保留只读：`plugin-boundaries-v3-full-01` 的运行时测试资源使用了不受该适配器支持的 libpq 连接字符串；`full-02` 捕获了新增时间注入夹具缺少 PG 作用域的问题。夹具改用可信作用域事务并要求恰好修改一行；不调整门槛、不跳过用例。修正后的同业务两组复验与任务 25 项实际 PG 验收均通过。

专项证据位于 `.local/plugin-boundaries-v3/generic-search-actual-01/`、`job-live-actual-03/`、`knowledge-recheck-01/` 和 `independent-review.md`。最终统一入口记录在下方；历史结果不作为本次通过的替代。

最终执行 `uv run --no-sync python scripts/check_project.py --local-integration --storage-integration --output .local/checks/plugin-boundaries-v3-full-03`：12 个门禁全部通过。离线合同 755 项，失败、错误、跳过均为零；真实存储集成 46 组，失败、错误、跳过、未执行均为零；运行时集成 2 项通过。PG、SQLite 各执行一条浏览器链路。工作区 Rust 测试 69 项通过。所有真实资源已清理，四个专属容器、源码构建的 MinIO 镜像、构建器及构建上下文已删除，共享 PG 容器保留。

## 支持限制

MySQL 仍支持通用 SQL、KV 和会话，尚无知识库领域方言。SQLite 的服务级隔离不等于 PostgreSQL RLS，也不是任意恶意 Python 插件的沙箱。原 PG Migration 1 哈希和授权策略保持不变，旧知识库 schema 仍保留历史搜索对象；不能用新搜索 Migration 1 覆盖原空间。换绑定不会自动搬迁数据。

领域事务最多 20,000 条语句，保留公共结果 10,000 行、8 MiB 与 30 秒限制，超限明确失败，不截断结果。通用 PG 检索每代际最多 10,000 条记录、64 MiB。测试使用合成工程证据；九个统一评分用例验证检索后真实上下文交付，不代表生产 RAG 准入成绩。新增外部模型、Embedding、裁判调用均为零。
