# BGE CrossEncoder 迁移验收（2026-10-29）

本轮将统一 RAG 框架的 BGE 本地组件探针迁到 Sentence Transformers `CrossEncoder.predict`，移除原有序列分类模型加载、手工 padding、forward 和 sigmoid 推理分支。框架接管模型执行，项目继续负责原文授权、完整输入准入、物理批次账本、稳定位置绑定和评分。

Qwen 使用不同的生成式 yes/no 协议，保留该必要适配；生产 Luna 的证据覆盖判断也保留。没有新旧 BGE 双实现或静默回退开关。该交付对应改造计划中的“专用 reranker 配对试验”，不是生产默认模型切换。

## 冻结范围与复用

- 历史来源：`reports/reranker-small-20261028/luna-migration-01/history`，48 道已见合成回归题；suite hash 为 `cab3945f809adf781439baa86f1743759aa3ce55729974584620506ba5c18d9c`。
- 核对历史 manifest 的 53 个文件；复用原文、候选、身份和冻结 ACL。未重新检索、生成答案或调用裁判。
- 旧 BGE 结果：`reports/reranker-small-20261028/luna-bge-cpu-01`。核对其 53 项 manifest、逐题请求/候选哈希、原文排序 trace、148 条实际批次及其输出和结算绑定；运行前均为 COMPLETE，无未决请求。
- 新结果：`reports/crossencoder-20261029-01`。固定上限 521 对候选、148 个本地批次、0 个远端调用，输出目录唯一。被测重排使用 live/under_test，不复用旧推理结果。
- 同一 BGE revision：`2cfc18c9415c912f9d8155881c133215df768a70`；CPU float32、8 线程、4 对/批次、512 token 上限，不截断。
- 旧运行是 PyTorch `2.14.1+cpu`，新运行是 `2.9.1+cu128` 在 CPU 上执行；Transformers 均为 `4.57.6`，新库为 Sentence Transformers `5.7.0`。因此不是只改变一个变量的性能实验，不能据此归因耗时差异。

旧结果以当前统一 runner/scorer 重新评分，写到新目录的 `old-bge-rescored/`，不沿用旧 PASS 作为结论。新旧 296 条批次的绑定核验、逐题分数/排序比较记录在 `paired-comparison.json`；其输入文件哈希记录在 `comparison-input-hashes.json`。本地原始产物按仓库规则不提交。

## 结果

| 指标 | 旧 BGE（当前重评） | 新 CrossEncoder |
|---|---:|---:|
| 固定题数 | 48 | 48 |
| 有候选并完成推理 | 47 | 47 |
| NOT_RUN（q072，上游无候选） | 1 | 1 |
| Top-5 片段保留完整证据 | 32/40 | 32/40 |
| Top-10 片段保留完整证据 | 37/40 | 37/40 |
| 来源级 Recall@5 | 0.916667 | 0.916667 |
| 来源级 MRR@5 | 0.941667 | 0.941667 |
| 来源级 nDCG 下界@5 | 0.903568 | 0.903568 |

40 是冻结集中有完整证据路径标注的问题数，未从中删除失败题。片段 Top-k 与去重来源 Top-k 分别计算。47/47 题完整排序和输入 token 计数相同，521 个位置分数最大绝对差为 `1.1622905731201172e-06`。

新增本地推理共 148 批、521 对，全部 COMPLETE；无 ERROR、未知预留或未决记录。新增远端模型、Embedding、judge 调用均为 0。新路径从分批循环开始至结束的记录时间合计约 94.67 秒，包含批次推理、账本和结果写入，不含模型加载、全题长度预检及 runner 评分；不当作线上端到端延迟或速度提升。

完整 RAG 验收仍为 47 FAIL、1 NOT_RUN、0 PASS：本组件没有生成答案，也没有观测最终模型请求，不能伪造补齐。合成回归不代表企业人工金标、当前线上 ACL 或生产准入。

## 交付决定

保留 CrossEncoder 作为唯一 BGE 推理实现。原候选顺序的 Top-5 完整证据为 40/40，BGE 为 32/40，因此不切换生产默认排序，也不将相似度当作证据充分性。本次证明库替换在该冻结集合上的排序一致性，没有证明专用模型提升业务质量。

新增确定性合同纳入 `local_rerank_contracts.py`，覆盖预算准入、每批预留、输出位置、超长拒绝、错误计费、取消/未决、终态禁止重发和本地固定模型加载。它们使用合成替身，和上面的真实权重执行分别报告。既有合同与生产覆盖判断未删除。

参考 [CrossEncoder 官方 API](https://sbert.net/docs/package_reference/cross_encoder/model.html)。后续计划仍可继续模型匹配的 token 计量；生产专用重排需要另行取得中文多跳、否定及表格完整证据的准入证据。
