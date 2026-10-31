# E2E 缺陷修复验证

## 两个原失败场景

2026-10-31 的增量真实测试只重跑 `dev-workflow` 和 `dev-enumeration`。两题均为 PASS，产品状态均为 `answered / complete`。原完整测试的 24 PASS、1 FAIL、1 ERROR 记录保持不变，本轮结果不覆盖旧结果。

流程题回答“下一步是 review”。枚举题返回九项资料。统一框架验证了最终证据、答案行为和引用，详细记录在本地 `reports/rust-main-host/fix-20261031-01/live/`。这两题是已见的合成工程回归，不代表企业人工金标或独立生产准入。

复用同一批不可变原件、已发布索引和既有 gold。本轮重新执行检索、重排、上下文装配和生成。新增 18 次物理请求，包括 15 次模型调用、3 次 Embedding 调用、0 次 judge 调用。模型输入 120,555 token，输出 3,227 token；Embedding 输入 86 token。新增请求没有 UNKNOWN、缺失用量或超预算。历史已审核 UNKNOWN 的费用和预留继续保留。原件没有重新上传，索引没有重建。

本次使用旧验收批次尚余的 21 次模型和 16 次 Embedding 限额。所有新请求均使用新的物理身份。没有自动重跑失败题。产品 Python 源码在测试期间未变化。Rust 宿主正常退出并报告 `completed`。账本及请求摘要在本地 `live-scope.json`、`live-summary.json`、`cumulative-budget.json`。

## 根因与确定性回归

上下文曾把 141 字符原文截成 140 字符，遗漏尾部空格，却声明原文仍未完整读取。新的选择逻辑先检查完整原文区间能否装入实际预算，再执行段落选择。字符边界和完整性标记由同一区间决定。防重复动作、步数上限和不完整工具结果保护保持原样。

规划调用曾收到答案引用协议，要求生成不属于规划 schema 的 claims 和 quotes。输出指令现在由当前 schema 所有权决定。每个逻辑调用独立拥有最多一次格式纠正，所有实际尝试仍共享原有 turn 预算和截止时间。非法 JSON 和额外字段继续被严格拒绝。传输错误不会触发格式纠正。

新增的原文窗口合同在修复前运行 6 项，出现 11 个失败断言。修复后与邻近合同共 24 项通过。模型调用合同在修复前为 3 FAIL、2 PASS，修复后与邻近合同共 56 项通过。主代理另行执行全部 11 项新合同，通过。独立审查执行新合同及 worker 合同共 18 项，通过。

确定性 Agent 场景使用会检查完整性标记的脚本模型，证明应用内的失败机制。真实模型两题复测证明当前冻结用例通过，不承诺模型未来不会重复动作或返回非法格式。

## 宿主审查

独立审查发现异常退出只终止直接 worker，会留下 worker 创建的后代进程。旧宿主已用真实子进程复现。修复通过 Windows Job 和 Unix 进程组统一拥有 worker 及后代，保留正常插件关闭，再有界清理残留进程。这属于宿主资源生命周期，不增加插件安全沙箱。

上述真实模型复测使用此前完整检查生成的 Rust 二进制及本轮修复后的 Python 源码。之后的进程树清理改动需要新的原生构建和集成验证，不能把这次付费结果宣称为后来二进制的完整重测。

## 最终本地检查与 PR

冻结源码后执行 `uv run --no-sync python scripts/check_project.py --local-integration --output reports/rust-main-host/final-check-20261031-02`，整体 PASS。包含 68 项 Rust 测试、558 项 Python 合同、16 项宿主集成、进程插件集成、2 项数据库集成和 24 个评分器控制场景。格式、Clippy、架构和 lint 均通过。

此前 `final-check-20261031-01` 保留 FAIL。测试目录包含完整用例名，导致一个插件工作目录长达 264 个字符，在 Windows 创建进程时失败。测试目录改用由用例身份派生的短标识，完整用例名保留在报告映射中。没有修改时间门槛或跳过失败用例。

使用最终检查生成的新宿主执行 `browser-20261031-02`，tester-army/e2e 的两项真实浏览器用例全部 PASS。报告明确记录模型 token 和模型步调用均为 0，宿主退出码为 0，关闭状态为 `completed`。产品远程模型和 Embedding 由运行配置禁用，此处不把配置得出的零调用数表述为费用账本实测。

PR 按顺序串联，均为非草稿，尚未合并。

1. [PR #22，Rust 主宿主与进程树所有权](https://github.com/Hubujiu/evidence-kb/pull/22)。
2. [PR #23，RAG 原文窗口与模型输出边界](https://github.com/Hubujiu/evidence-kb/pull/23)。
3. [PR #24，必需数据库与 tester-army 浏览器 CI](https://github.com/Hubujiu/evidence-kb/pull/24)。

Linux 目标通过本地交叉编译和 Clippy。Linux 实际进程组与浏览器运行结果以 GitHub CI 为准，发布记录时仍在执行。运行方法见 [浏览器 CI](browser-ci.md)。本地原始付费请求和诊断未提交到公开仓库，CI 自行生成并上传合成检查产物。

## 原则与审查限制

Model the Domain 让完整性取决于原文区间，让纠正额度属于逻辑调用，让宿主拥有进程树。Fix Root Causes 保留停止与校验规则，修复产生错误状态的位置。Boundary Discipline 将输出校验和数据库地址校验留在输入边界。Encode Lessons in Structure 将必需 CI 依赖与结果绑定变成反例测试和 lint。

Test Behavior, Not Implementation 要求核对实际请求、用户收到的状态、下载原件和进程存活。Prove It Works 要求新原生制品通过完整检查及浏览器复测。Sequence Work into Verifiable Units 保留先提交失败用例再提交修复的历史。Separate Before Serializing Shared State 让代理独占改动文件，由主代理串行处理 Git。Laziness Protocol 保留现有区间和评分器，避免增加重复抽象。

独立 GPT 代理审查了实现和本地证据，提出的问题均已修复。跨模型审查尝试被平台任务访问限制阻止。没有可用的完整会话文件，因此审查未声称完成逐条对话审计。Windows Job 绑定失败路径通过源码审查，未执行故障注入。
