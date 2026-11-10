# 已发布版本的 Embedding 查询准入

审阅基线：`f0ea7450b33b21022fbb61e91c1539d5a290c89b`。

## 问题与状态边界

更新文档会产生新的候选版本。用户显式构建并发布之前，查询应该继续使用旧的已发布版本。`Platform.search` 和 Agent 检索管道已经按此规则选择原文，但公开 Embedding 插件的 `KnowledgeAccess.validate_embedding` 始终读取当前候选快照。因此，发布 v1 后上传 v2，会导致 dense/hybrid 查询在调用模型之前以 `STALE_BUILD: Embedding source content changed` 失败；BM25 仍可查询 v1。

这里存在两个独立维度：内容所属版本，以及当前是否允许访问和处理该内容。历史 manifest 固定内容身份，不固定旧权限。

| 操作 | 文档 Embedding 的内容 | 查询 Embedding 的内容 | 权限来源 |
| --- | --- | --- | --- |
| 已发布 v1 | 当前 staging build 的候选内容 | v1 | 当前 ACL、绑定和处理策略 |
| 上传 v2，尚未发布 | v2 | v1 | 当前 ACL、绑定和处理策略 |
| 构建 v2，尚未发布 | v2 | v1 | 当前 ACL、绑定和处理策略 |
| 发布 v2 | 后续 staging build 的候选内容 | v2 | 当前 ACL、绑定和处理策略 |
| 撤销读者、绑定或相关处理权限 | 拒绝不再允许的操作 | 拒绝不再允许的操作 | 当前权限立即参与准入 |

修复只在 `KnowledgeAccess.validate_embedding` 的快照调用中增加 `published=task == 'query'`。document 任务继续读取候选版本，query 任务读取当前 manifest 的不可变源版本。原有 build 状态、authority revision、源版本/parse ID/文本哈希、provider/purpose 与最终快照复核仍然执行；证明缓存命中前仍验证当前 authority。没有新增持久状态、数据库格式或 API 字段。

## 公开合同回归

`kb_platform/rag_testing/knowledge_embedding_contracts.py` 使用真实 SQLite authority、EmbeddedSearch 和公开 `EmbeddingClient → AuxiliaryModelEndpoint → KnowledgeAccess` 调用链；Agent 路径还经过 `EvidenceClient → ServiceEndpoint → EvidenceEndpoint`。向量提供者是明确的固定二维合成替身。

新增的两个合同覆盖以下观察：

- resumable 与 legacy 两种构建路径中，dense、hybrid 和 Agent dense 查询在候选更新和构建之后仍返回 v1，发布后才返回 v2；每次实际 query 审计绑定正确的 manifest、source version、parse ID 和文本哈希。
- document Embedding 实际收到 v2 文本，并绑定新的 staging build；没有错误复用旧发布内容。
- 在候选已经不同的情况下，当前读者撤销、绑定移除、Embedding 关闭、LLM context 关闭和 Embedding provider 移除仍然有效。旧 proof 的 audit 被拒绝；使用最新 authority revision 的 audit 同样按当前权限拒绝。拒绝后合成 provider 发送记录数量不增加。

在修复前运行同一新增合同，7 个子场景都在候选更新后的查询处复现该 `STALE_BUILD`，原有公开 Embedding 合同通过。修复后模块全部 3 个测试通过。另运行 formal combination、公开应用组合、文档管理、发布 Embedding 和租约合同共 55 个测试，全部通过。

这些是组件与公开 JSON 边界的确定性合同，不是实际 HTTP 网络、PostgreSQL、远程 Embedding 或生产 RAG 质量测量。新增付费调用数为 0。

## 有限模型及映射

`authority_publication` 家族增加 `EmbeddingSourceView` 小模型：两个内容版本、一个已发布源、一个 staging build、一次候选上传、一次撤权和一个请求。它区分任务内容视图与当前权限，并保留 revision/build 检查。说明及明确排除范围见 [模型说明](../formal/authority_publication/README.md)。

新增有限正模型检查 `TaskSpecificSourceView` 和 `AdmissionUsesLivePolicy`。错误 query 视图负控复现本问题；冻结旧权限负控是主动注入的安全错误。两个独立 witness 使用正确配置，实际到达“候选 v2 与已发布 v1 不同，但查询 v1 被准入”和“document 构建 v2 被准入”的状态。

使用固定 Java 21 和锁定 TLC 实际执行：SANY 审阅通过；正模型通过，产生 204 个状态、152 个不同状态且队列耗尽；两个负控和两个 witness 均得到指定不变量的 exit 12。新增五个 Python 源锚点；没有机械刷新其他家族摘要。`combination-inputs-embedding-v7.json` 追加精确输入和合同身份，历史 v1–v6 清单保持原样。

这没有建立 Python 到 TLA 的机械精化证明。多文档、同版本的多个 build、外部存储故障、缓存并发及网络交付时的授权仍需对应合同和集成验证。

## 本地检查结果与限制

| 检查 | 结果 |
| --- | --- |
| 新旧 Embedding 公开合同 | 修复前明确复现；修复后 3 / 3 通过 |
| 相关五个合同模块 | 55 / 55 通过 |
| 修改的 Python 文件 Ruff | 通过 |
| 整个登记表的直接 formal lint | 通过，无违规 |
| 新有限模型的 SANY、TLC 正负控与 witness | 全部得到预期结果 |
| `uv run --no-sync python scripts/check_project.py` | 已执行，FAIL；本环境 owned process runner 在启动子命令前返回 `RuntimeError` |
| `uv run --no-sync python scripts/check_project.py --formal-only` | 已执行，FAIL；相同子进程所有权启动限制 |

根审阅在未改动的基线上确认了同样的环境限制。必需入口没有降级、绕过或忽略失败；直接定向检查不替代这两个完整门禁。合并前仍须由支持其进程监督协议的 CI 环境通过完整检查。
