# Evidence KB · 文档证据知识库

以原文证据为中心的知识库，提供文档解析、权限校验、检索、带引用的单会话问答，以及统一 RAG 验收框架。

当前版本为 `0.2.0`，回答方案固定为 `evidence_release_candidate`。这是本地部署和工程验证项目，合成测试通过不代表企业业务金标验收或生产准入。

## 能力

- 原件上传、多格式解析、原文子单元检索与引用定位。
- PostgreSQL pg_textsearch BM25 与 pgvector 精确向量检索，以及需要显式配置的 Google Embedding 和候选池重排。
- 表格计算、原文流程查询、单会话恢复与请求幂等。
- 身份与来源权限复核、精确缓存、物理请求账本和有界预算。
- 同一套 Suite、Plan、Trace、runner 和 scorer，支持冻结 trace 回放及显式实时适配。

## 环境

离线验证需要 Python 3.12 或 3.13 和 [uv](https://docs.astral.sh/uv/)。运行文档工作台还需要 Docker Compose。远程模型、Embedding、Docling 和 OCR 不属于离线检查的前置条件。

```powershell
uv sync --locked --group dev
uv run --no-sync python scripts/check_project.py
```

输出默认保存在新的 `.local/checks/` 子目录。该命令检查架构边界、Ruff、组件合同和 24 条合成评分器控制，期间阻断 Python 网络发送和子进程启动。它不访问真实模型或生产数据。

## 启动本地文档工作台

```powershell
uv run python scripts/bootstrap.py
docker compose up -d --wait
uv run uvicorn kb_platform.api:app --host 127.0.0.1 --port 58080
```

打开 [文档工作台](http://127.0.0.1:58080/)，使用 `.local/identities.json` 中生成的本地令牌。该文件包含私有令牌，不要提交。

Compose 默认只启动 PostgreSQL，镜像包含固定版本的 BM25 和向量扩展。正文、向量、索引和发布状态由数据库持久化，原件文件仅供备份下载。Compose 数据库仅绑定回环地址，包含公开开发密码；生产部署需要单独配置认证、网络、数据库凭据和备份。事务与恢复边界见 [检索存储](docs/search-storage.md)。

已有 OpenSearch 数据必须先显式导入，不能只切换环境变量。导入、回退和备份步骤见 [检索存储](docs/search-storage.md)。

未配置远程模型时，Agent 对话入口会拒绝生成请求。文档管理和 BM25 检索不依赖模型密钥。

复杂 PDF 的自动路由可能需要独立 Docling 环境。Linux 上通过 `EVIDENCE_KB_DOCLING_PYTHON` 指向其 `bin/python`，需要其他解析器时使用 `EVIDENCE_KB_DOCLING_OCR_PYTHON` 或 `EVIDENCE_KB_RAPIDOCR_PYTHON`。只使用原生解析时，设置 `EVIDENCE_KB_PARSE_ROUTE=native`；这不提供扫描件 OCR。

## 配置远程提供者

将 `config/remote-providers.example.json` 或 `config/vertex-providers.example.json` 复制到 `.local/providers.json`，替换端点、模型和凭据。示例中的模型名称需要你的提供者实际支持，仓库不提供模型账户或额度。

PowerShell 中显式启用后重启服务。

```powershell
$env:KB_REMOTE_MODELS = '1'
$env:KB_PROVIDERS_CONFIG = '.local/providers.json'
uv run uvicorn kb_platform.api:app --host 127.0.0.1 --port 58080
```

上传、索引和问答可能产生付费请求。执行真实验收前冻结输入、明确预算，并核对已有账本。固定交付工厂 `kb_platform.delivery:create_delivery_app` 还要求匹配的交付配置和私有运行目录。Windows 的 `scripts/start_delivery.ps1` 属于该高级交付流程，需要显式提供配置参数；本机交付配置不随仓库发布。

## CI 与贡献

GitHub Actions 在 PR、main/master 推送、合并队列及手动触发时运行相同检查。Windows 和 Ubuntu 分别验证 Python 3.12、3.13，并由 Gitleaks 扫描 Git 历史中的凭据，`required` 汇总任务要求全部检查成功。CI 不配置提供者密钥，也不执行付费评测。

- [开发与合并检查](docs/development.md) 说明命令、测试取舍及分支保护。
- [模块架构](docs/architecture.md) 说明交付、产品和评测模块边界。
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
