# 请求恢复与任务范围：上游对照

本轮针对 PR #15 的历史 UNKNOWN 和多轮中间轮 `partial`，先查固定提交源码，再修改现有消息投影与提示。没有引入新 Agent loop、重试执行器、评分器或供应商结算协议。

## 固定参照与采用边界

| 项目 | 查到的具体行为 | 本项目采用及差异 |
| --- | --- | --- |
| [Codex `f752c97` 中断上下文](https://github.com/openai/codex/blob/f752c97f7d0be0cbef5cece327f52a16ad6fb1c6/codex-rs/core/src/context/turn_aborted.rs) | 中断后明确提示工具可能已经部分执行、后台进程可能仍活动。 | 保留执行不确定性；中断不能当成零发送或零费用。已有物理账本及生产者控制继续负责这一边界。 |
| [Codex 同版本默认指令](https://github.com/openai/codex/blob/f752c97f7d0be0cbef5cece327f52a16ad6fb1c6/codex-rs/protocol/src/prompts/base_instructions/default.md) | 在已有项目中按用户要求精准修改，计划用于跟踪执行；不把相关但未要求的改进扩成任务。 | 任务要求来自用户请求及有效历史，不来自规划器自行扩充的子问题。这里借鉴范围约束，不声称 Codex 有同款 RAG 核验器。 |
| [pi `4b535d5` Agent loop](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/agent/src/agent-loop.ts#L251) | `error`/`aborted` 保持失败终态；正常工具循环、steering/follow-up 消息和 `finishTurn` 的继续决定分别处理。 | 运行终态与业务答案完整性分别报告，不把结束一次运行等同于全部语义需求通过；继续复用现有图、状态与核验流程。 |
| [DSH `ce7073d` 重试实现](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm-retry/src/index.ts)及[说明](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm-retry/README.md) | 在持久步骤边界恢复；等待前记录 retry，发送前记录 retry-started；每次重试是可能再次计费的新请求。直接 stream 消费者仍单次执行。正常模式有限重试，另有显式 always 模式。 | 复用现有请求身份、计量和有限预算。不照搬 always 模式，也不以“上游会重试”为由跳过本项目 UNKNOWN 审核要求。新请求成功不能结算旧未知费用。 |
| [DSH Agent 输入及提交边界](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent/README.md) | 用户/steering 输入带来源，完成的 assistant 消息与失败 attempt 分别持久化；瞬时输出不是恢复依据。 | 用户请求、规划派生内容、候选答案自评各有来源。核验以实际用户输入和当前证据为依据，不把派生内容升级为用户要求。 |

以上是这些具体文件支持的行为，并非对三个项目所有功能的穷尽审计。它们不是供应商账单系统，所查恢复路径没有提供本项目供应商历史请求的执行结果。历史 UNKNOWN 的外部核实仍需可关联的供应商响应或账单证据；不能从一次新请求、缓存命中或本地错误类型推出旧请求未执行。

## 从原始请求定位到的根因

旧会话中，用户要求“请只按新的来源回答热线”。物理请求 `01695ade4fd448a6944a5ba9551942bb` 的规划结果新增了“热线适用于哪些事项”；后续读取沿用该子问题，`43f590e8c2a84dee8a2ead6269b504af` 的答案因缺少适用事项自评 partial，`3232a064448847199a45f01af1d50860` 的核验继续认同这一自拟要求。错误首先发生在任务分解，并经共享 `work_state` 和候选自评传递，不是缺少热线证据。

本轮改动：

- 共享任务指令要求子问题对应用户目标或必要前提；当前用户缩小范围时，不继承已被替代的计划要求。影响事实含义的实体、日期、条件、例外仍须保留，不能靠缩减真实需求换取 complete。
- `work_state` 的派生子问题、缺失信息与充分性判断只进入规划调用。最终回答和核验仍看到当前用户问题、有效历史、真实工具结果、当前授权原文；上下文证据选择也以当前请求为准。
- 核验候选投影不再包含回答器的 `completion`/`unresolved` 自评，避免把候选自己的评分当成独立核验依据。完整答案正文和全部 claims 仍接受核验，包括额外作出的断言。
- 不根据关键词把 partial 改成 complete，不跳过核验，不新增“核验器的核验器”。用户确实要求但证据缺失的部分仍返回 partial。

新增合成合同在来源切换后注入过度扩展的规划结果，核对最终模型收到的真实投影；对照“只问规则”和“另要求缺失到期日”两个用户请求，分别保留 answered 和 partial。它验证输入边界及状态传递，不把模型替身当成真实语义质量证明。

## 验收范围

本机统一检查 `1338019817fe43db83f402f221fe7953` 通过：374 项 Python 合同、46 项 Rust 测试、24 项评分器控制，以及架构、lint、格式、Clippy 和构建。

真实增量验收计划保存在 `reports/pr15-upstream-comparison-20261030-01/scope.json`：使用原来冻结的两组七轮会话、相同 gold 与来源策略，创建新会话执行当前组件。复用原文和索引，不增加预算批次或独立裁判调用。当前批次运行前 32 条请求均 COMPLETE，剩余硬上限为 91 次模型、29 次 Embedding、1,276,171 输入 token 和 196,601 输出 token；这些 token 是保守估算预算，实际 usage 单独记账。历史 UNKNOWN 及其预留保持原样。

第一轮 `conversations-01` 的最终统一合同为 2/2 PASS，但逐轮读取发现来源切换中间轮误澄清：模型将《澄川-pptx-规则.pptx》的通用文件名当成“不含联系信息”的依据，在零检索下要求重新选择。该轮报告保持只读，最终 PASS 不掩盖中间失败。进一步明确可信来源控制的含义：新 LOCKED 是对已选新来源的检索限制，不是未完成切换；文件名不能证明内容缺失，须先检索当前选择。

只对受影响的来源切换三轮执行 `source-switch-02`，复用原始 case/gold，统一合同 1/1 PASS，三个实际轮次均 answered/complete、unresolved 为空。中间轮先检索新来源并返回 `400-010-7711` 和工作日 `08:30 至 18:00`，没有重复授权或虚构适用事项要求。预算更正四轮使用第一轮已经成功的结果，保留各阶段源码快照；没有声称最后一版又重跑了全部七轮。

本轮共真实执行 10 个用户轮次，增量采纳的七轮均 answered/complete，520→680→520 的用户陈述归属和新来源热线逐轮核对完成。这是对当前合成场景的原始回答检查，不是独立语义裁判或对任意问题的保证。新增 42 个物理请求（38 模型、4 Embedding）全部 COMPLETE，usage 增量为 274,966 输入 token、3,955 输出 token、108 Embedding 输入 token；无新索引或 judge 调用、无新增 UNKNOWN。17 个前序快照均重新核验通过，原有一个 UNKNOWN 与费用预留不变，货币金额未测量。`final-audit.json` 记录逐轮文件哈希、请求与账本绑定及源码快照。

最后代码的统一门禁 `cf2a9e7e798b4d2285e93e267f494c3d` 通过，仍为 374/46/24；没有删除合同或降低门槛。
