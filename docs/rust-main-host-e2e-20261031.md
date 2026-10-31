# Rust 主宿主完整 E2E：2026-10-31

本轮按用户要求执行当前 Rust 主宿主下的完整既有验收集。结果为 **FAIL：26 个场景中 24 PASS、1 FAIL、1 ERROR，全部已执行**。没有用复测或旧成功记录替换本轮失败。

## 范围与结果

| 检查 | 结果 |
| --- | --- |
| 开发集，12 道单轮题 | 10 PASS、1 FAIL、1 ERROR |
| 保留集，12 道单轮题 | 12 PASS |
| 两组真实会话，共 7 个用户轮次 | 2 PASS；7 轮均 answered/complete |
| 两次最终 HTTP 幂等回放 | 返回完全相同，无新增物理请求 |
| 独立数据库的真实 HTTP、解析子进程、建索引、检索、引用及权限 | 2 项集成检查 PASS |
| 两次 Rust 宿主正常关停 | completed，根生命周期已清理 |

线上路径为 Rust `evidence-host` → Python worker → 根插件组合 → 知识库图流程 → 公共模型服务 → 原有提供商适配器与物理账本。真实提供商使用冻结的 `codex/gpt-6-luna` 和 Vertex `gemini-embedding-2` 配置，不使用 MockTransport。

复用已有不可变原件及两个已发布索引，重新核对原文、当前权限和 authority 绑定；复用了 66 条上传/建索引/发布回执，新上传/建索引/发布请求为 0。检索、重排、上下文装配、生成、引用和会话均在当前进程实际执行。独立数据库集成另以合成 Markdown 原件真实执行上传至引用链路，不把历史多格式索引复用称为重新解析了全部原件。

本轮固定计划及结果在本机 [scope.json](../reports/rust-main-host/e2e-20261031-01/scope.json)、[summary.json](../reports/rust-main-host/e2e-20261031-01/summary.json)。各组沿用统一 runner/scorer；原始 trace、最终请求采集和物理绑定位于相应 `dev`、`holdout`、`conversations` 目录。

## 两个失败

**dev-workflow：FAIL。** 当前状态为 intake、licensed=yes。检索至最终上下文的必要证据全部齐全，回答“下一步是 review”，产品核验也认为答案有支持。但流程因 `repeated_action` 到达边界，最终保留 `partial`，因此统一评分器报告 `answer_behavior_not_verified`。这是完成状态问题，没有把有证据的局部回答冒充任务完成。

**dev-enumeration：ERROR。** 规划阶段先收到非法 JSON，经原有一次格式修复后继续执行；后续规划响应又违反 Schema，包含不允许的额外字段，最终返回 `INVALID_AGENT_OUTPUT`。当前上下文估算 27,059，小于 118,784 的输入预算。失败响应的最终证据捕获还记录 `UNTRACKED_FINAL_EVIDENCE`，统一 trace 保留产品根因，未伪造最终生成请求观测。

诊断记录见 [failure-diagnostics.json](../reports/rust-main-host/e2e-20261031-01/failure-diagnostics.json)。本轮未修改 RAG 行为、Schema、评分规则或验收门槛，也未重跑这两题。后续应分别分析重复工具动作与完成状态的关系，以及规划器在枚举任务中的结构化输出稳定性；现有证据不足以将失败归因为 Rust 主宿主引入的确定性回归。

## 会话、计费与运行状态

逐轮查看原始响应：用户预算按 520 → 680 → 520 更正，最终明确归于用户提供的信息；来源锁从门禁规则切换到值班资料后，正确回答新来源热线和服务时段。最终引用仍按当前来源验证。这里是原始响应复核，没有新增独立语义裁判或企业人工金标认证。

新增 193 个物理请求全部 COMPLETE：模型 165 次，Embedding 28 次，judge 0 次。新模型输入 1,217,615 token、输出 25,607 token；Embedding 输入 876 token。新请求无 UNKNOWN、无缺失 usage、无重复物理身份，全部与统一账本逐项匹配。供应商价格和货币费用未测量。

两批标准有限预算上限合计为模型 240 次、Embedding 64 次，未超限。完整保留所有前序费用与 1 个已审核历史 UNKNOWN，累计状态为 `REVIEWED_UNKNOWN`，不是全部结算。前序未漂移、未知预留未清零，见 [physical-audit.json](../reports/rust-main-host/e2e-20261031-01/physical-audit.json) 和 [cumulative-budget.json](../reports/rust-main-host/e2e-20261031-01/cumulative-budget.json)。

实时逐场景适配器累计耗时约 662 秒，包含会话各轮，排除准备与宿主重启；它不是单请求延迟，也不用于宣称性能提升。291 个源码/配置文件执行前后哈希一致。测试创建的两个 Rust 服务进程均已正常停止。

启动时旧提供者配置中的顶层 `cache_mode` 被当前严格配置校验拒绝；生成新私有配置移除该过时字段，两个提供者仍保持禁用响应缓存、传输重试为 0，原配置保留。语义页预检按当前实现生成新 manifest 绑定；原语义页存储没有已物化胶囊，没有将旧胶囊改标为新版本。常规已发布向量索引的身份及当前权限通过重新绑定校验。

LLM 已从知识库业务中拆为公共模型服务、插件注册、配置和供应商适配器。当前实现仍在同仓 Python 模块中，尚不是独立发行包，也没有改写为 Rust；Rust 管理其生命周期与调用租约。跨语言结构化生成通过 `llm.complete_json` 接入同一实现，详见 [模型服务](model-services.md)。
