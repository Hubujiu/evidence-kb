# Evidence KB · 文档证据知识库

以原文证据为中心的知识库，提供文档解析、权限校验、检索、带引用的单会话问答，以及统一 RAG 验收框架。

当前版本为 `0.2.0`，回答方案固定为 `evidence_release_candidate`。这是本地部署和工程验证项目，合成测试通过不代表企业业务金标验收或生产准入。

## Rust 插件基座

项目按 DeepSeek Harness / Cordis 的服务边界组织，以 Rust 实现生命周期与公共协议；不依赖 TypeScript 运行时。`plugin-runtime` 管理作用域、依赖、服务和资源，`plugin-host` 管理进程，`agent-harness` 负责应用装配。默认 loop/graph 位于独立 `agent-services`，统一 LLM 和存储协议分别位于 `llm-service`、`storage-service`，通用内核不依赖这些可选服务。

知识库由配置绑定业务插件，具体模型 SDK、数据库驱动和知识库算法作为实现接入。现有 Python 模型与存储调用路径使用 Rust 公共协议检查；Rust LLM SDK 另提供真实异步流接口。当前内置 HTTP/Luna 供应商仍使用同步响应，前端尚未提供增量流输出。固定上游版本、所有权、用量语义及接入方式见 [Rust 插件与服务基础](docs/dsh-rust-foundation.md)。

## 能力

- 原件上传、多格式解析、原文子单元检索与引用定位。
- PostgreSQL pg_textsearch BM25 与 pgvector 精确向量检索，以及需要显式配置的 Google Embedding 和候选池重排。
- 表格计算、原文流程查询、单会话恢复与请求幂等。
- 身份与来源权限复核、精确缓存、物理请求账本和有界预算。
- 同一套 Suite、Plan、Trace、runner 和 scorer，支持冻结 trace 回放及显式实时适配。

## 环境

项目支持 Linux，Windows 用户在 WSL 的 Linux 环境中运行后端和检查命令。离线验证需要 Python 3.12 或 3.13、C 链接器、[uv](https://docs.astral.sh/uv/) 和 [Rustup](https://rustup.rs/) 管理的 Rust 工具链。使用 PostgreSQL、MySQL、Redis 或 S3 服务时需要相应服务；本地默认组合使用 SQLite、embedded 与 files。远程模型、Embedding、Docling 和 OCR 不属于离线检查的前置条件。

```bash
rustup toolchain install 1.99.0 --profile minimal --component rustfmt --component clippy
cargo fetch --locked
cargo build --workspace --locked --offline
uv sync --locked --group dev
uv run --no-sync python scripts/check_project.py
```

输出默认保存在新的 `.local/checks/` 子目录。该命令检查架构边界、Ruff、Rust 格式与测试、Python 组件合同、独立进程插件、Rust 主宿主和 24 条合成评分器控制；合同工作进程阻断 Python 网络发送和子进程启动，进程及本地 HTTP 检查单独执行。它不访问真实模型或生产数据。

默认服务由 Rust 主宿主启动，运行前先完成上述构建。更新 Rust 源码后重新执行 `cargo build --workspace --locked --offline`。宿主同目录的原生库提供单次 Agent 运行的值状态；插件生命周期由主进程维护。部署与跨语言接入见 [Rust 主宿主](docs/rust-main-host.md)。

## 启动本地文档工作台

首次启动或修改 Web 前端后，先使用 Node.js 22 构建界面：

```bash
cd web
npm ci
npm run build
cd ..
```

```bash
uv run python scripts/bootstrap.py
uv run --no-sync cargo run --locked --offline -p agent-harness --bin evidence-host -- --profile config/host-profiles/api.json --stop-file .local/api.stop
```

打开 [文档工作台](http://127.0.0.1:58080/)，使用 `.local/identities.json` 中生成的本地令牌。该文件包含私有令牌，不要提交。

正常停止时，在另一 Linux 终端执行 `touch .local/api.stop`；下次启动前移除该标记。宿主等待在途工作与资源清理，异常退出返回非零状态。HTTP 路由和构建后的 Web 页面由现有 Python 应用提供。知识库管理支持创建知识库、上传解析、发布 BM25 索引、检索及原文定位。

`bootstrap.py` 显式初始化新的本地存储空间，并生成含数据身份的 `.local/storage.json`；已有文件不覆盖。新的知识库应用不兼容历史业务数据和配置，使用下面的知识库初始化命令创建空空间。切换后端可按 [业务数据重置](docs/storage-reset.md) 创建新的业务空间，旧目录及费用账本不删除。配置其他后端时，使用 [公共存储配置](docs/storage-plugins-execution-plan.md)，BM25、dense、正文与原件可分别绑定。

已有 OpenSearch 数据必须先显式导入，不能只切换环境变量。导入、回退和备份步骤见 [检索存储](docs/search-storage.md)。

未配置远程模型时，Agent 对话入口会拒绝生成请求。文档管理和 BM25 检索不依赖模型密钥。

复杂 PDF 的自动路由可能需要独立 Docling 环境。Linux 上通过 `EVIDENCE_KB_DOCLING_PYTHON` 指向其 `bin/python`，需要其他解析器时使用 `EVIDENCE_KB_DOCLING_OCR_PYTHON` 或 `EVIDENCE_KB_RAPIDOCR_PYTHON`。只使用原生解析时，设置 `EVIDENCE_KB_PARSE_ROUTE=native`；这不提供扫描件 OCR。

## 配置远程提供者

多提供商及 OCR、Embedding、语音、生图接入使用新的 [公共模型服务](docs/model-services.md)，
配置示例为 `config/models.example.json`。设置 `KB_MODELS_CONFIG` 后，`foundation.llm` 装配提供者和既有物理账本；Rust 统一拥有模型注册与调用状态，知识库通过公开 `llm.service` 合同调用。也可选用下面的 Luna/Google 提供者实现。

将 `config/remote-providers.example.json` 或 `config/vertex-providers.example.json` 复制到 `.local/providers.json`，替换端点、模型和凭据。示例中的模型名称需要你的提供者实际支持，仓库不提供模型账户或额度。

显式启用后重启服务。

```bash
uv run python scripts/bootstrap.py --application knowledge
export KB_REMOTE_MODELS=1
export KB_PROVIDERS_CONFIG=.local/providers.json
uv run --no-sync cargo run --locked --offline -p agent-harness --bin evidence-host -- --profile config/host-profiles/api.json --stop-file .local/api.stop
```

知识库默认使用 [`config/applications/knowledge.json`](config/applications/knowledge.json) 的 11 个显式节点：通用 LLM 与存储底座，加上知识库入口、工作流、上下文、会话、权限、权威数据、模型策略、模型领域视图、文档与检索。Rust 根据合同版本、能力及 `bindings` 编译依赖图并管理生命周期。提供者配置位于 `plugins.llm.config`；知识库模型节点只配置路由、策略与应用预算。设置 `KB_APPLICATION_PROFILE` 可选择另一份应用图。职责与适配算法见 [底座与应用架构](docs/foundation-architecture.md)，公开 JSON 和跨语言执行见 [插件合同](docs/plugin-architecture-v4.md)，独立消费者与真实本地 wire 检查见 [验收说明](docs/foundation-acceptance.md)。

默认知识库数据配置为 `.local/storage-knowledge.json`；`KB_STORAGE_CONFIG` 可显式覆盖。初始化命令不会复制旧知识库、索引或会话。没有发生自动迁移，也不会因连接错误清空数据。

上传、索引和问答可能产生付费请求。执行真实验收前冻结输入、明确预算，并核对已有账本。固定交付工厂 `kb_platform.delivery:create_delivery_app` 还要求匹配的交付配置和私有运行目录。本机交付配置不随仓库发布。先用 `uv run --no-sync python scripts/delivery_control.py start --check-only` 校验配置，再去掉 `--check-only` 启动；使用 `--config`、`--providers`、`--semantic-runtime` 和 `--run-root` 指定本机路径。启动输出包含进程回执路径。停止时执行 `uv run --no-sync python scripts/delivery_control.py stop --receipt <回执路径>`。依赖启动仅在指定 `--start-dependencies` 时执行。

## CI 与贡献

GitHub Actions 在 PR、main/master 推送、合并队列及手动触发时运行相同检查。Ubuntu 验证 Python 3.12、3.13，并由 Gitleaks 扫描 Git 历史中的凭据，`required` 汇总任务要求全部检查成功。CI 不配置提供者密钥，也不执行付费评测。

- [开发与合并检查](docs/development.md) 说明命令、测试取舍及分支保护。
- [模块架构](docs/architecture.md) 说明交付、产品和评测模块边界。
- [插件宿主](docs/plugin-harness.md) 说明 Rust loop、服务注入、profile 和插件生命周期。
- [Rust 主宿主](docs/rust-main-host.md) 说明进程入口、执行环境协议和跨语言插件。
- [统一 RAG 评测](docs/rag-testing-framework.md) 说明证据、缓存、费用和观测要求。
- [协作约定](AGENTS.md) 适用于源码修改与评测。

新增必要回归放在 `kb_platform/rag_testing/*_contracts.py`。不要另建独立评分器或预算账本。删除测试时说明重复行为和保留用例。

## 公开范围

公开仓库包含产品源码、合成控制夹具、示例配置和 CI。以下内容不发布。

- `.local/` 中的令牌、提供者配置、原件、索引、会话和费用账本。
- `reports/` 中的历史实验结果与审计证据。
- `output/`、`outputs/` 中的独立仓库、下载内容和生成产物。
- 本机交付配置、虚拟环境与下载的代理技能。

文档中引用的历史路径描述原有实验，不是随源码附送的测试数据。离线组件合同与评分器控制可以直接运行；历史回放和线上业务质量评测需要另行提供数据。

## 许可证

项目使用 [MIT License](LICENSE)。内置 PageIndex 函数保留其原有 MIT 许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。
