# 可靠性与工作台整合

本次将可靠性修复、RAG 功能和实际 React 工作台收敛到同一候选，交付为 [PR #85](https://github.com/Hubujiu/evidence-kb/pull/85)。验收以该 PR 当前提交的 `required` 及其全部依赖为准，旧分支的成功记录不代替当前结果。

## 整合输入

| 输入 | 精确提交 | 用途 |
| --- | --- | --- |
| main | `69bc7d4d9868289a649b70c8946bd292f8951eb1` | 保留 #78 的统一检查与源码质量要求，以及 #81 的 uv Action 更新 |
| #74 | `150d2b215fed60846d3630b8cffc495f307ea944` | 统一错误、403 封禁、原子授权与会话完成、进程所有权、有限模型及组件合同 |
| #82 | `13d2cf9000da998307553a1451bb929a54ad7376` | RAG 证据与预算边界、解析与建库改动、React 工作台 |
| #83 | `e730f39598004661d4b3461a85d3c994c62d5a3e` | 请求代次、最终写回检查、Blob 释放及延迟响应回归 |

#31、#74、#82、#83 的提交是本候选的祖先。#33 至 #73、#75 至 #77 的 44 个当前 head 与 #74 的冻结输入精确匹配，由其组合实现和验收义务承接。逐项记录见 [PR 映射](pr-integration-pr-map-20261110.json)。后者是语义整合，不能把它写成 44 次独立 Git 合并。

#84 的 Java 与 Node Action 更新独立保留。此 PR 不关闭旧 PR，也不把候选提交等同于合入 main。

## 修复后的边界

模型失败使用 `model_errors.py` 的同一 DTO。发送事实、已观察 usage、物理请求 ID、存储 operation ID 和预算 operation ID 分开保留。`EmbeddingCallError` 也消费该 DTO，避免把 `BUDGET_INSUFFICIENT` 误换成外层错误码。

提供者返回 403 后，先记录封禁，再完成费用结算。结算确认丢失时，后续请求仍受封禁约束。预算预留确认丢失则保留已存在的未知预留，模型发送次数为零，失败事件保留恢复身份，不自动重发。

会话完成保留必填 `authority`，通过共同物理事务锁定授权与写入。工作流的业务错误不能进入成功提交。`COMMIT_UNKNOWN` 继续走原恢复路径，不能追加普通失败回合或再次生成答案。

工作台在请求、后续请求、父组件回调和 Blob 创建前校验身份、知识库、视图和操作代次。取消传输是辅助措施，迟到响应仍须通过最终写回检查。新操作不会假定已经发出的服务端写入已撤销。

统一检查先执行 TypeScript 检查并构建当前前端，再启动会访问页面的应用集成。`/` 和 `/agent` 的资源检查绑定 `web/dist/index.html`。诊断控制台在 `/console`，Agent 诊断控制台在 `/agent/console`。旧控制台的有限模型与竞态测试仍约束其实际实现，新工作台的行为由 `web-ownership` 和实际 React 浏览器用例单独验证。浏览器还冻结下载释放定时器，验证工作台卸载时同时撤销 Blob 和取消定时器。

## 验证记录与复现

在 Linux 或 WSL 中安装锁定依赖。Node 版本与 CI 保持一致，使用 `22.23.1`。

```bash
uv sync --locked --group dev
npm ci --ignore-scripts --prefix web
npm ci --ignore-scripts --prefix e2e
node e2e/node_modules/@e2e-dev/web/dist/bin.js install chromium --with-deps
uv run --no-sync python scripts/check_project.py --web-integration
uv run --no-sync python scripts/install_formal_tools.py
uv run --no-sync python scripts/check_project.py --formal-only
```

实际数据库、存储和浏览器验收继续由 CI 的同一入口开启 `--local-integration --storage-integration --browser-integration --web-integration`。这些选项不替代默认完整门禁。FAIL、ERROR、跳过、超时和不完整发现均不能通过。

历史 `combination-inputs-v1.json`、Linux v2、authority v3、composition v4 和浏览器 v1 记录保持原样。新的 [product v5](../formal/combination-inputs-product-v5.json) 逐项列出模型、配置、入口和用例变化；[源码审查记录](pr-integration-source-review-20261110.json) 记录对应理由。Rust 绑定变化仅来自注释删除，审查脚本比较了全部非注释代码行。

知识库工作流模型增加受限输入纠正路径，保留原有所有不变量和正模型的 `MaxSteps=2`。新增负控检查不安全纠正与重复纠正，另有可达性控制验证合法纠正确实能发生。已有真实图合同覆盖只读、无副作用、无费用、步数、答案预算和直接计算限制。

六项曾被 #82 清理的 `ModelEmbeddingReviewContracts` 用例恢复保留，避免丢失组合清单要求的授权、原请求上下文、已知未发送及预算释放合同。本次整合没有通过删除失败用例、降低门槛或忽略退出码换取通过。

## 交付范围

本次用户明确选择 Linux 后端与 CI，Windows 通过 WSL 使用。原生 Windows 的进程监督、Tantivy 文件访问及历史超时问题不宣称已经修复。

受控浏览器、SQLite、远端 CI 中的实际数据库和存储合同使用合成输入。有限 TLC 结果和这些合同不等同于生产 RAG 质量金标、外部模型兼容性或全系统精化证明。正式提供者、生产故障注入及复杂 OCR 工具链仍须按各自资源与预算执行验收。此次修复不需要新增付费模型、Embedding 或 judge 调用，也不改动已有费用与未知预留。
