# 统一 RAG 评测框架

公开仓库包含合成控制夹具和可运行的离线合同。文中引用的 `reports/`、`.local/`、历史账本和原文快照不随源码发布。历史回放、PageIndex 完整流程及真实提供者验收需要调用方另行准备相应输入，不能从公开仓库推断这些流程已经复现。

当前保留 `kb_platform/rag_testing/` 的合同、runner、scorer、精确缓存、预算账本、语义复核和产品 HTTP 适配器。独立 pytest 测试与历史实验运行器已退出活动项目；它们的原始源码与执行日志在精简快照中保留。必要的组件和框架回归统一放在本包的 `*_contracts.py`，由 `scripts/check_project.py` 自动发现。该入口分别报告组件合同和冻结 trace 的评分器控制，详见 [开发与合并检查](development.md)。

## 评测边界

固定 Suite、Plan、原文哈希、版本、身份和 ACL 修订。金标和 qrels 只进入评分器，不能进入被测产品。完整证据使用原文摘录与偏移绑定，并以 AND/OR 表达必需或替代证据路径。最终上下文应在应用裁剪之后、实际请求发送之前采集；没有发送观测时必须标为 replay。

固定分母保留 PASS、FAIL、ERROR、NOT_RUN、UNCERTAIN。分别报告候选召回、最终证据、答案、引用与权限；评分器控制样本不能当作产品质量成绩。历史权限重构不能冒充当前线上权限验证。

`StageCache` 使用包含实现、配置、上游有序输入、语料/索引、模型、身份/ACL、问题/历史和 sample_id 的精确依赖键。每次命中仍校验完整性和当前授权。被测阶段 live 下使用 `under_test=True`，cold 全部绕过。不能用同一回答的重复回放测模型随机性。

`BudgetLedger` 与 `LinkedBudgetLedger` 同时服务实际产品运行。失败、重试、UNKNOWN 和未知预留都必须保留；已有 COMPLETE、ERROR 或 UNKNOWN 请求不可盲目重发。新增付费执行前确认授权、预算和调用上限。

## 保留入口

| 入口 | 用途 |
|---|---|
| `scripts/rag_test.py freeze` | 验证 Suite 并冻结 Plan |
| `scripts/rag_test.py replay` | 对冻结 Trace 执行当前评分 |
| `scripts/replay_rag_history.py` | crossdoc-hard 历史请求绑定、当前装配对照；不发送模型请求 |
| `scripts/replay_delivery_context.py freeze/run` | 只读原文快照上的保留/装配对照 |
| `scripts/run_delivery_acceptance.py` | 原件夹具、工作量估计、显式上传构建、HTTP 产品验收 |
| `scripts/run_delivery_conversations.py` | 单会话真实多轮验收 |
| `scripts/verify_delivery_operations.py` | 只读实际 HTTP 运维验收 |

旧 `rag_test.py live` 的竞品耦合适配入口已移除。需要新的 live 场景时复用 `runner.run(..., adapter=...)` 或现有 HTTP 适配器，不另建评分器或费用账本。

统一 SDK：

```python
from kb_platform.rag_testing.runner import run

# adapter 只接收 AdapterInput、授权原文和 corpus hash；不接收金标。
report = run(suite, plan, output, adapter=adapter)
```

所有输出使用尚不存在的新目录；旧结果保持只读。只改评分或报告时直接重评原 Trace，不重新调用检索、生成或语义裁判。语义复核需要独立正负控制校准；未经校准或缺失判断不得默认通过。

## 最终上下文证据目标

Plan 可设置 `objective="evidence"`，以授权原文、证据完整性、最终上下文映射、预算和已知禁止内容为验收项，不以模型答案或拒答行为作为门槛；不能同时要求 `require_answer_behavior=True`。默认 `full` 保持旧计划的规范化哈希兼容。不可回答题通过完整性检查，不代表已经提供充分答案证据。

2026-10-28 的五项人工裁定保存在 `reports/rag-evidence-20261028/annotations-v2/`，原始标注与 trace 保持只读。公开题已经用于开发，不能继续称为独立验收数据。

`python -m kb_platform.rag_testing.evidence_experiment --suite <新版suite.json> --output <新目录> --history <旧运行目录>` 使用统一 runner/scorer，复用旧 trace 并真实执行当前证据选取与 ContextManager；可重复提供 `--history`。`--source-catalog <文件>` 接受经过原文一致性校验的冻结标题目录。输出属于当前组件回放，不代表线上检索、实时 ACL 或供应商收到的最终请求。

产品检索配置默认 `title_navigation=true`；显式设为 false 可关闭管线中的扩展。普通 Agent `search` 也通过词法管线执行，不额外调用 Embedding 或模型重排。先保留原排序证据，再补导航；预算不足时保留完整子单元并记录遗漏，不把不完整父级标为完整。标题、缩写和显式章节链接只是导航线索，不构成实体关系证明；交付仍经过 canonical unit 读取和现有权限检查。

Agent 服务及 HTTP API 的默认输入额度统一为 24000（保守 UTF-8 估算），输出预留 1500、安全预留 1024，仍受模型窗口约束；显式环境配置和 ContextPolicy 保持优先。原先 12000 额度可能被 Schema 与引用元数据占满，不能据此归因为模型窗口硬性不足。

Markdown/TXT 解析器 `native-text/0.3.0-markdown-it` 使用 markdown-it-py 识别结构，继续将管道表格及紧邻说明保留为原文块。原文偏移和哈希不从渲染文本推算，解析快照纳入库版本。版本变化只影响新解析；旧快照保持不变，导航同时支持旧的逐行表格单元，不要求静默重建旧索引。其他格式的原生解析版本仍为 `native-evidence/0.2.1`。

边界回归：`python -m kb_platform.rag_testing.repair_contracts` 和 `python -m kb_platform.rag_testing.default_path_contracts`。`python -m kb_platform.rag_testing.default_path_replay --suite <suite.json> --catalog <sources.json> --output <新目录> --service` 通过真实 HTTP 路由、服务、网关及 ContextManager 检查模型入参，采用内存存储/搜索夹具和受控模型替身，统一 runner/scorer 评分。它不等于真实数据库/搜索服务或远程供应商验收。该模式按完整序列化消息 24000 codepoint 上限评分，不能直接与仅证据投影预算比较。

## 当前精简证据

见 [本轮报告](../reports/ablation-prune-20261027/report.html)。保留/装配对照仅 4 道已见历史题、相同槽位和预算；四组合均通过，不支持全局等价结论。240 条 crossdoc-hard 历史结果重新核对请求并评分，前后结果相同，旧失败仍保留。历史答案、引用及费用不被覆盖为新生成结果。

后续业务质量验收仍需冻结业务样本和实际请求观测。现有合成夹具与公开资料不等同于企业人工金标或生产准入。

## 本地小模型重排组件对照

`python -m kb_platform.rag_testing.local_rerank --history <统一框架历史目录> --output <新目录> --models qwen3-06b --device cuda`
校验历史 manifest、原文、身份和冻结 ACL 后，真实执行本地重排，复用统一 runner、scorer、StageCache 和 BudgetLedger。
可选模型还有 `bge-base`；权重 revision 固定。使用 `uv sync --locked --group dev --extra reranker` 安装可选的本地推理依赖（Sentence Transformers、PyTorch、Transformers 4.x 与 sentencepiece），默认服务和统一离线检查不加载这些模型。依赖锁中的 PyTorch 版本固定在本轮验证过的 2.9.1；GPU 构建需匹配本机环境并在运行 manifest 中记录。
BGE 使用 Sentence Transformers 5.7 的 `CrossEncoder.predict`，删除原来的 `AutoModelForSequenceClassification` 手写推理路径。Qwen 保留其生成式 yes/no 协议，不套用序列分类分数。模型加载只读取本地固定 revision，BGE 禁用远程自定义代码和隐式凭据。
输入超长明确报错，不静默截断。没有候选的历史错误保留为 NOT_RUN，不编造候选或重试旧请求。
长度预检包含 query、document 和特殊 token，超长在任何批次准入前拒绝；库推理也显式关闭截断和附加 prompt。每次最多 4 对候选、一次库批次，由原有账本先预留再执行。失败保留预留，取消留下未决身份；COMPLETE、ERROR 和未决批次均不得自动重发。排序使用输入位置绑定分数并稳定处理同分，原始片段及其版本、哈希、权限绑定不变。

本轮 [CrossEncoder 迁移记录](crossencoder-migration.md) 保存配对结论。专用模型分数没有替代 `validate_coverage` 的引用和充分性合同，也没有启用产品默认 reranker。

该入口不调用答案生成或语义裁判，不采集最终模型请求，所以完整验收仍不能 PASS。
`summary.json` 单独报告原文证据在候选池、前 5/10 个片段中的覆盖，以及前 5 个去重来源的 IR 指标；两种 k 的单位不同。
未知相关性只给 nDCG 下界。缺失/失败样本不从固定分母移除，分数与实际请求账本分别报告。
历史 ACL 只适用于冻结的本地快照，不表示当前线上授权已验证。

`python -m kb_platform.rag_testing.research_history` 是 research-v2 的只读薄迁移入口，校验原冻结查询哈希，并将保存的排序输出与原文目录绑定。
原标注只到文档级；迁移后的证据含义是完整交付该原文，不是新增的语义或精细 span 金标。未恢复的物理请求身份会明确记为未观测。

## PageIndex 有界接入试验

`python -m kb_platform.rag_testing.pageindex_probe --suite <suite.json> --gold <scorer-gold.json> --providers <private-config.json> --predecessor <ledger.sqlite3> --output <新目录> [--execute]`

复用统一 runner、scorer、物理请求采集及 LinkedBudgetLedger；多个旧账本使用重复的 `--predecessor` 参数。默认只冻结预检；执行使用新的输出目录。最多 12 道开发题、每题两组共 4 次模型调用，禁止自动重试，Embedding/judge 上限为 0。每组最终上下文上限相同，为 16000 Unicode codepoints；导航组另有两次检索模型调用，不能宣称计算预算相同。

实验采用 VectifyAI/PageIndex 提交 `b81465d9ad3d80ee1df344508a85d1f2cdfb8a26` 的三个原样 Markdown 建树函数，保留 MIT 许可于 `kb_platform/vendor/`。两轮导航、原文前缀目录、节点到原文偏移的绑定和标题短包合并是本项目适配器；不代表官方完整 SDK agent、摘要建树或 PDF 流程。不会自动启用产品默认检索。

基线是已有实验 `title_packet`；增量组优先保留基线，再补导航选中的原文节点。超预算按完整片段丢弃并记录，不改写原文。导航只接收授权语料，不接收 gold。所有新增推理禁用缓存；旧结果不能作为新采样。最终模型请求有物理绑定，证据完整和答案 EM 分别报告；语义正确性和引用支持性仍为未测。已见开发题和已知标注缺陷不能作为独立业务验收。

离线边界回归：`python -m kb_platform.rag_testing.pageindex_contracts`。

## 官方 PageIndex 长 PDF 流程

`kb_platform.rag_testing.pageindex_full_index` 调用独立 `.venv-pageindex` 中安装的固定上游 SDK，执行完整默认 Flash PDF 建树、full 优化、节点摘要和文档描述。`pageindex_full_query` 调用官方 SDK agent 的目录导航、读页和生成，对照 BM25（k1=1.5、b=0.75，前 8 页）及相同模型的读取器。该基线是组件级实验，不是生产 HTTP 流程。模型实际发送均经 `sdk_gateway` 的本地透明转发和统一 MeteredTransport、LinkedBudgetLedger 留痕。

执行前使用 `pageindex_pdf_fixtures` 冻结公开长 PDF 的原文页、作者编写的跨章节问题和评分侧答案。实际计划及阶段边界见 `reports/pageindex-full-20261028/`。金标不得传给 SDK。新运行使用不存在的输出目录；不能原样重跑已有付费执行。索引失败恢复可重复传 `--recover <旧目录>`，仅当代码、原件、配置和实际请求依赖匹配时复用 COMPLETE 响应；终态失败的显式重试使用新物理身份并关联原请求，未知预留保持计费。官方 SDK 会吞掉部分摘要异常，因此不能只看 `submit_document` 的 completed 就宣称全部模型阶段成功，还要检查网关账本和本地拒绝记录。

工具型 agent 的原文可能分布于多条消息。Trace 新增可选 `request_evidence_bindings`，按实际消息索引和 JSON 叶子路径绑定每个 final fragment；路径中的 `$json` 表示解析一层 JSON 字符串。在此模式下 `final_context` 是用于评分的原文投影，不冒充实际发送的连续上下文。评分器核对所有绑定、原文哈希、来源权限和完整性，并以**全部序列化 request_messages 的 Unicode codepoints**核对预算。原来的连续 context 模式保持原语义；两种协议不能混用预算成绩。树摘要仍是导航数据，不代替原文证据或校准后的语义支持结论。

离线回归：`python -m kb_platform.rag_testing.request_projection_contracts`，再执行原有评分器控制回放。
