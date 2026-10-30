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

Rust 工具链由 `rust-toolchain.toml` 固定，依赖由 `Cargo.lock` 锁定；新环境先下载固定依赖，随后检查使用离线构建。workspace 包含通用生命周期 `agent-kernel` 和插件/loop 实现 `agent-harness`。统一入口执行原生格式、Clippy（警告视为错误）、离线 release 构建和测试。每轮在独立 target 目录构建，成功后把动态库复制到本轮输出的 `native/`，合同进程只加载这个副本。构建失败或制品缺失时，合同报告 `BLOCKED`、整体 `FAIL`，不会加载环境指向的旧库；其他门禁继续收集结果。部署构建见 [Rust 运行时](rust-agent-runtime.md)，profile 与扩展接口见 [插件宿主](plugin-harness.md)。

默认结果写入 `.local/checks/<唯一编号>/`。指定目录时必须使用尚不存在的路径。

```powershell
uv run --no-sync python scripts/check_project.py --output reports/ci/my-new-run
```

退出码 0 表示所有门禁通过。任何架构违规、lint 错误、合同失败、合同加载失败、零用例模块、跳过、预期失败或非 PASS 的评分器控制结果都会使命令失败。输出目录已存在时拒绝执行，不覆盖旧证据。父进程会继续收集其他门禁的结果，便于一次定位多个问题。

`summary.json` 记录各检查退出码和日志路径。`contracts.json` 单独报告组件合同。`scorer-controls/report.json`、`junit.xml` 和 `report.html` 由统一 RAG runner 生成。评分器控制复用冻结 trace，不代表产品召回、答案质量或远程请求验证。

检查过程将运行目录指向临时目录，禁用默认 API 初始化、远程模型和 Embedding。合同和控制回放在独立 Python 工作进程中执行，导入前安装审计钩子，阻断 socket 连接、DNS 查询、UDP 发送和子进程启动。依赖安装需要联网，但检查不调用提供者。这个约束防止误执行，不是恶意代码沙箱。

合同协调进程按排序后的模块列表分配四个隔离工作进程，各自使用独立临时运行目录、日志及审计钩子。协调进程不加载产品模块；统一入口仍限制整个合同阶段为 180 秒，子进程限制为 170 秒以留出收尾时间。聚合器要求全部发现模块恰好执行一次、每个模块用例数非零且与报告总数一致。缺失分片、重复模块、子进程非零退出以及失败、错误、跳过、预期失败均不能通过；不通过放宽时间门槛消除 Windows 落盘等待导致的超时。

## 修改或新增插件

先声明 `PluginSpec` 的 `provides`、`requires` 和版本，再把实现加入可信 catalog。profile 的 `use` 只能引用已登记的键，不能写 Python 导入路径或任意代码。默认知识库 profile 在 `config/agent-profiles/knowledge.json`；`KB_AGENT_PROFILE` 允许启动时选择另一个配置。

业务请求通过 `host.call(service, method, ...)` 取得运行租约。`host.service()` 用于装配，不应保存原始服务并绕过生命周期直接处理请求。插件创建的资源用 `ctx.defer()` 注册释放；借用父作用域或外部调用者的资源不能重复取得关闭所有权。child 覆盖父服务时必须在该条配置的 `overrides` 中声明服务名。

关闭先进入 draining；已有请求可以收尾，新请求被拒绝。仍有活动请求时关闭返回忙碌错误，等待请求结束后再关闭，不能靠重试业务调用完成卸载。清理失败保持失败状态和尚未释放的资源，不能报告插件已卸载。现阶段采用新 profile 启动新实例，再排空旧实例，不提供运行中透明热替换。

相关合同至少覆盖服务替换、依赖缺失/循环、启动失败清理、作用域隔离或运行中关闭中的实际变更风险。默认 `harness.sessions.memory` 是单次运行的临时会话插件；需要持久会话时必须提供明确的持久化适配器，不能把它当作知识库 `KnowledgeSessions` 或物理请求账本。

## 新增或删除测试

把必要合同放在 `kb_platform/rag_testing/*_contracts.py`，使用 `unittest.TestCase`。质量入口自动发现每个模块，并要求每个模块至少有一个测试。不要另建 `tests/` 或新评分器，也不要把测试名维护在另一份手工命令列表中。

每个测试必须验证一个可观察合同。例如原文内容、权限拒绝、预算预留、持久化结果、退出码或实际请求绑定。预期值来自需求，不能由被测实现重新计算。测试可使用临时原文和受控模型替身，但必须注明合成性质。

不用裸 `assert` 承担 unittest 断言。不要提交跳过或预期失败来换取绿色 CI。新场景继续复用统一 `Suite`、`Plan`、`Trace`、runner 和 scorer。组件检查与 RAG 质量评分分开报告。

删除测试前，记录被删除用例的行为、保留用例的位置及两者的覆盖关系。只覆盖重复行为的用例可以合并参数。不能因当前实现失败而删掉权限、预算、证据完整性或请求身份的边界检查。

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
