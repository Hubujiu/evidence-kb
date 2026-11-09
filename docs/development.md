# 开发与合并检查

## 运行与 CI 相同的检查

在项目根目录执行以下命令。

```powershell
rustup toolchain install 1.99.0 --profile minimal --component rustfmt --component clippy
cargo fetch --locked
cargo build --locked --offline
uv sync --locked --group dev
uv run --no-sync python scripts/check_project.py
```

Rust 工具链由 `rust-toolchain.toml` 固定，依赖由 `Cargo.lock` 锁定；新环境先下载固定依赖，随后检查使用离线构建。workspace 包含通用生命周期 `plugin-runtime`、进程监督 `plugin-host`、loop/graph 与可执行入口 `agent-harness`，以及单次运行值状态和 C ABI `agent-kernel`。统一入口执行原生格式、Clippy（警告视为错误）、离线 release 构建和测试。每轮在独立 target 目录构建，成功后把动态库、`evidence-host` 和原生 worker 夹具复制到本轮输出的 `native/`。构建失败或制品缺失时，相关阶段报告 `BLOCKED`、整体 `FAIL`，不会加载旧制品；其他门禁继续收集结果。部署构建见 [Rust 主宿主](rust-main-host.md)，profile 与扩展接口见 [插件宿主](plugin-harness.md)。

默认结果写入 `.local/checks/<唯一编号>/`。指定目录时必须使用尚不存在的路径。

```powershell
uv run --no-sync python scripts/check_project.py --output reports/ci/my-new-run
```

退出码 0 表示所有门禁通过。任何架构违规、lint 错误、合同失败、合同加载失败、零用例模块、跳过、预期失败或非 PASS 的评分器控制结果都会使命令失败。输出目录已存在时拒绝执行，不覆盖旧证据。父进程会继续收集其他门禁的结果，便于一次定位多个问题。

`summary.json` 记录各检查退出码和日志路径。`contracts.json` 单独报告组件合同。`scorer-controls/report.json`、`junit.xml` 和 `report.html` 由统一 RAG runner 生成。评分器控制复用冻结 trace，不代表产品召回、答案质量或远程请求验证。

检查过程将运行目录指向临时目录，禁用默认 API 初始化、远程模型和 Embedding。合同和控制回放在独立 Python 工作进程中执行，导入前安装审计钩子，阻断 socket 连接、DNS 查询、UDP 发送和子进程启动。依赖安装需要联网，但检查不调用提供者。这个约束防止误执行，不是恶意代码沙箱。

合同协调进程按排序后的测试用例列表分配四个隔离工作进程，各自使用独立临时运行目录、日志及审计钩子。协调进程不加载产品模块；统一入口仍限制整个合同阶段为 180 秒，子进程限制为 170 秒以留出收尾时间。每个工作进程在安装离线审计钩子后独立发现完整用例集合；聚合器要求各进程发现集合相同、每个模块非零、全部用例恰好执行一次且与报告总数一致。缺失分片、重复模块、子进程非零退出以及失败、错误、跳过、预期失败均不能通过；不通过放宽时间门槛消除 Windows 落盘等待导致的超时。

## 修改或新增插件

生命周期、配置 schema、进程插件和显式恢复接口以 [插件运行时](plugin-runtime.md) 为准。`plugin-runtime` 仅依赖固定的 Serde、serde_json 和 Tokio；harness 通过 workspace 路径依赖它。通用运行时不得反向依赖 Agent 或知识库，精确边界由 checker 和架构合同检查。

普通合同仍禁止联网和启动子进程。正式 checker 另外编排受控进程插件与 Rust 主宿主 fixture，包含临时 loopback HTTP 健康检查；这些阶段使用合成数据和本地模型响应。完整知识库 HTTP、解析子进程和 PostgreSQL 验收使用 `--local-integration` 并设置 `KB_INTEGRATION_DSN` 与同库的 `KB_INTEGRATION_MIGRATION_DSN`；前者为受限运行账号，后者只供受控夹具初始化与清理。数据库名须以 `rust_plugin_acceptance_` 开头，且连接 loopback。这个入口保留相同的所有离线检查，不会自动调用真实模型。

知识库应用使用 `config/applications/knowledge.json`：实例指定 implementation/version/config，依赖通过 bindings 绑定消费者别名到提供者具名出口。可信 catalog 声明合同 ID、主版本、能力、requires 和执行方式；Rust 在挂载之前检查缺失、冲突及循环。`KB_APPLICATION_PROFILE` 选择应用配置。公开方法只接收和返回 JSON；原始对象、数据库连接、Python 类和回调不能跨业务插件边界。实现入口见 `knowledge_capabilities.py`，协议及替换示例见 [插件架构](plugin-architecture-v4.md)。

`PluginSpec` 是本地执行器内部及独立文档工作台的低层装配 API，不是知识库用户需要编辑的节点。其存储需求统一放入 requires；不再存在独立 storage_slots 字段。内部组件合同仍直接验证这些底层部件。

业务请求通过 `host.call(service, method, ...)` 取得运行租约。`host.service()` 用于装配，不应保存原始服务并绕过生命周期直接处理请求。插件创建的资源用 `ctx.defer()` 注册释放；借用父作用域或外部调用者的资源不能重复取得关闭所有权。child 覆盖父服务时必须在该条配置的 `overrides` 中声明服务名。

关闭先进入 draining；已有请求可以收尾，新请求被拒绝。仍有活动请求时关闭返回忙碌错误，等待请求结束后再关闭，不能靠重试业务调用完成卸载。清理失败保持失败状态和尚未释放的资源，不能报告插件已卸载。配置通过显式 apply/update 排空受影响子图，再激活新代次；切换不透明复用旧绑定，清理失败时保留受阻状态。

相关合同至少覆盖服务替换、依赖缺失/循环、启动失败清理、作用域隔离或运行中关闭中的实际变更风险。默认 `harness.sessions.memory` 是单次运行的临时会话插件；需要持久会话时必须提供明确的持久化适配器，不能把它当作知识库 `KnowledgeSessions` 或物理请求账本。

## 新增或删除测试

把必要合同放在 `kb_platform/rag_testing/*_contracts.py`，使用 `unittest.TestCase`。质量入口自动发现每个模块，并要求每个模块至少有一个测试。不要另建 `tests/` 或新评分器，也不要把测试名维护在另一份手工命令列表中。

每个测试必须验证一个可观察合同。例如原文内容、权限拒绝、预算预留、持久化结果、退出码或实际请求绑定。预期值来自需求，不能由被测实现重新计算。测试可使用临时原文和受控模型替身，但必须注明合成性质。

不用裸 `assert` 承担 unittest 断言。不要提交跳过或预期失败来换取绿色 CI。新场景继续复用统一 `Suite`、`Plan`、`Trace`、runner 和 scorer。组件检查与 RAG 质量评分分开报告。

删除测试前，记录被删除用例的行为、保留用例的位置及两者的覆盖关系。只覆盖重复行为的用例可以合并参数。不能因当前实现失败而删掉权限、预算、证据完整性或请求身份的边界检查。

`authentication_contracts.AuthenticationContracts.test_http_uses_replaceable_ttl_store_without_reading_identity_file` 已完整迁至同模块 `live_suite()` 的 `AuthenticationIntegration`，由统一入口的受控 `host-integration` 阶段执行。有效身份、无文件启动、503、撤销后 401、缺失 token 401 和资源关闭断言全部保留。Windows 的 asyncio 内部唤醒 socketpair 使用 loopback connect，因此 ASGI 用例不能放在禁止所有 socket.connect 的离线工作进程中；网络禁令、超时和通过门槛保持不变。纯认证逻辑及插件准入仍由离线合同执行。

本轮把 `repair_contracts.PackingContracts.test_unmatched_bridge_is_not_arbitrarily_cut` 合并至 `default_path_contracts.DefaultPathContracts.test_unmatched_bridge_can_use_spare_context_budget`。保留两种文本长度及预算上限断言。超大表格不可截断的独立用例继续保留。历史归档的 208 个测试不恢复，也不删除其证据。

## 运行需要外部资源的验收

数据库、搜索服务、Docling、OCR、本地模型权重和远程提供者不属于默认离线门禁。涉及这些组件的变更，在离线检查通过后执行对应验收。不要把替身测试通过称为真实集成验证。

按 [统一 RAG 规范](rag-testing-framework.md) 先检查活动运行、冻结计划、实际请求账本及精确缓存。确认本轮授权和剩余预算后，仅执行必要增量阶段。付费调用必须显式执行，不能挂入 PR 自动检查。

## 配置远端合并约束

`.github/workflows/ci.yml` 在 PR、main/master 推送、合并队列和手动触发时执行。Windows 与 Ubuntu 分别检查 Python 3.12、3.13。四个质量任务使用同一入口，`secrets` 任务使用固定版本 Gitleaks 检查 Git 历史。聚合任务 `required` 仅在质量任务和密钥扫描全部成功时通过。工作流不使用路径过滤，以免必需检查因路径未命中而缺失。

工作流只授予 `contents: read`，不持久化 checkout 凭证，不读取模型密钥。Action 固定到完整提交 SHA。Dependabot 每周检查 Action 更新。安装使用固定 uv 版本及 `uv sync --locked --group dev`，不静默更新 `uv.lock`。失败时仍上传检查产物，保留 14 天。

将项目关联到目标 GitHub 仓库后，在默认分支的 ruleset 中执行以下配置。

1. 要求通过 PR 合并。
2. 将 `required` 设为必需状态检查。
3. 要求至少一名 reviewer 批准，并在新提交后撤销旧批准。
4. 要求解决审查讨论。
5. 禁止强制推送和删除默认分支，并按团队实际权限配置绕过者。

仓库管理员通过 GitHub 设置启用这些规则。工作流配置文件本身不会自动启用分支保护。实际运行结果以仓库 Actions 页面为准。

工作流语义参考 [GitHub 触发事件文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow) 和 [uv 的 GitHub Actions 集成文档](https://docs.astral.sh/uv/guides/integration/github/)。

## 代码精简约束

统一检查入口同时执行源码质量检查和 Ruff。Python 源码及脚本不得包含注释或文档字符串；命令行帮助使用显式字符串，接口说明写入文档。禁止恢复 `agent_store`、`search_compat` 模块及其导入。字符串中的 Markdown 标题、URL 和测试夹具不受注释规则影响。

Ruff 拒绝可变默认参数、无效表达式、重复类型条件、可直接返回的布尔分支、可合并的相同分支和隐式 Optional。新增规则必须提供能检出违规、接受合法代码的合同，不用忽略规则或降低门槛消除失败。

项目只支持当前数据和配置格式，不新增历史字段回退、旧格式自动升级或历史 hash 放行表。合法性校验放在输入边界，内部函数复用已验证的合同；权限复核、事务租约和外部响应验证仍按实际边界执行。新增 hash 必须有明确消费者及用途，例如内容完整性、精确缓存或请求幂等；不能只写入诊断输出。审查必须检查这些语义约束，lint 不保证识别跨函数重复校验或判断 hash 是否有业务价值。

本轮删除的旧存储选择入口、失效性能脚本及构建恢复用例迁移映射见 [存储能力清理](storage-capability-cleanup.md)。当前验收不调用 `postgres_operations` 或 `embedded_operations`，旧报告中的这些名称属于历史入口。

## 统一 CLI 与确定性验收

首次安装开发环境时执行 `uv sync --locked --group dev` 和 `npm ci --ignore-scripts --prefix web`。Node 使用 CI 固定的 22.23.1。默认命令仍为 `uv run --no-sync python scripts/check_project.py`，执行全部离线合同、评分器控制、Rust 检查、受控进程集成、Ruff、跨语言无注释检查、解析器合同和 TypeScript 检查。

`uv run --no-sync python scripts/check_project.py --list` 只列出自动发现的合同模块和可选阶段，不导入产品、不执行测试、不创建输出目录。它不是验收结果。

额外阶段通过同一入口执行。所有选项保留默认完整门禁，不提供跳过失败检查的开关。

| 选项 | 验证范围 | 前置条件 |
| --- | --- | --- |
| `--local-integration` | HTTP、解析和 PostgreSQL | 两个隔离验收 DSN |
| `--storage-integration` | 实际存储提供商及其浏览器验收 | `KB_STORAGE_TEST_*` 受控资源 |
| `--browser-integration` | Rust 主宿主、HTTP、PostgreSQL 浏览器验收 | `--local-integration`，已安装 e2e 依赖与 Chromium |
| `--web-integration` | 合成聊天与工作台浏览器验收 | 已安装 web、e2e 依赖与 Chromium |

安装浏览器依赖使用 `npm ci --ignore-scripts --prefix e2e`，随后从 e2e 目录执行 `node node_modules/@e2e-dev/web/dist/bin.js install chromium --with-deps`。Web 阶段会构建当前前端，并使用本轮 Rust 快照。浏览器阶段仅在前置门禁通过后执行，使用不可变的 `prerequisites.json`。最终 `summary.json` 包含请求执行的阶段，前置失败时记录 BLOCKED，不遗失该阶段。

CI 的 integration 任务使用同一命令并开启全部四个选项。删除质量检查步骤、将任务变为可选或移除任一集成选项，都会使 CI 配置合同失败。远端分支保护仍由仓库管理员配置，不能仅凭工作流文件宣称已经启用。

无注释检查覆盖 Python、Rust、JS、TS、TSX、CSS 和 HTML 内嵌脚本。字符串中的 URL、Markdown、注释字符和测试夹具合法。第三方许可证、依赖、构建产物与冻结证据不作为活动源码扫描。Python 禁止 docstring，接口说明放在文档。前端类型检查同时拒绝未使用变量和参数。

集成运行器要求执行前计划非零、实际数量相等，且无失败、错误、跳过、预期失败或意外成功。浏览器还要求单次尝试、非零确定性断言和零模型调用。这些合成合同证明测试协议及组件行为，不替代生产 RAG 质量评测。

### 显式交付模型配置

`enterprise-kb-delivery/1` 保留历史 Luna 固定模型约束。`enterprise-kb-delivery/2` 允许配置声明一个生成模型标识，并要求它与私有 provider 文件完全一致；容量、单并发、无自动重试、禁缓存和费用校验保持相同约束。版本2不暗示模型能力已验证，也不允许把不同模型结果当作相同模型的配对性能结论。模型选择只在交付配置边界处理，workflow、data 和答案插件不依赖具体模型名称。
