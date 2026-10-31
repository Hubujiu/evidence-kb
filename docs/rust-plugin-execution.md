# Rust 插件改造执行记录

> 历史记录说明：本文的完成与 455/65/24 检查叙述适用于审查前 `105d664`，final-04 性能冻结于 `5d18294`。后续用户复核确认八项缺陷，原完成结论已失效。当前修复和验收状态见 [审查修复记录](rust-plugin-review-fixes.md)，旧结果保持原样，不代表修复后性能已通过。

执行输入为 2026-10-30 的 `evidence-kb-rust-plugin-execution-plan.md`。工作从 `cbd8623f4907ae36e0e1b65bb990fa3e33875620` 开始。历史报告保持只读，模型、Embedding 和语义裁判新增调用预算为零。

## 完成判定

Rust 独占实例、代次、依赖、准入和清理状态。Python 仅执行动作并持有业务对象。正式入口使用同一运行时，现有会话、账本、权限、请求身份和事务规则保留。正式检查使用本轮构建的原生库。外部集成、平台矩阵和性能未取得实际证据时分别记为未验证。

## 工作顺序

1. 读取 poteto-mode 的 Principles，固定当前代码、参考来源、行为和性能基线。
2. Ground。检查旧宿主、领域构造、入口与验收边界。
3. Sketch。比较串行 coordinator 动作队列和每实例异步 actor。
4. Agree。按已授权计划直接推进，选择动作队列。
5. Implement。先贯通 Rust 和 Python 会话，再扩展竞争、依赖、作用域、事件。
6. 将业务构造改为显式端口，迁移模型、基础设施、解析、任务、检索、工具与组合。
7. 统一 profile、配置应用和默认入口，删除旧生命周期权威。
8. 接入独立进程插件。Wasm 仅在计划要求的实际需求成立后实施。
9. Scrap。若多个实现边界持续需要绕过，回到设计并删除错误结构。
10. 运行正式门禁、必要集成和对照，审计证据，按工作包提交和创建 PR。

每个实现单元先写可观察合同，再运行对应检查。验证结果只有 VERIFIED、NOT VERIFIED 和 INCONCLUSIVE。不能以编译成功替代业务验证。

## 当前系统与设计选择

实施前的 Rust host 负责整棵 scope 的依赖顺序和租约，Python 负责 mount 顺序、资源栈和最终 ready/disposed 通知。这使 Rust 无法独立验证部分初始化和清理是否真的完成。实施后由一个 Rust coordinator 决定生命周期；Agent loop、graph、业务请求账本继续作为各自领域的既有权威。

候选 A 将一个 Rust coordinator 建模为串行状态归约器，短调用派发 Init、Invoke、Cancel、Cleanup，Python 在锁外执行并回报。候选 B 为每个插件建立 Tokio actor，通过异步消息协调依赖与 Python 回调。按唯一状态所有权、同步 C ABI 适配、失败可观察性、交错可测试性和接口复杂度选择 A。B 的异步业务执行仍可通过同一动作协议提供，不为生命周期再建 actor 权威。

公共调用保持 `PluginHost(profile, catalog)`、`host.call(service, method, ...)`、`host.close()`。`PluginContext` 把资源立即登记给 Rust。运行时分配实例、代次、操作、注册、资源和租约身份。初始化迟到后只清理旧代次；调用取消只通知，实际回报前仍持有租约；清理失败保留剩余资源和依赖。父资源仅借用，子 scope 不取得关闭所有权。

业务构造选择显式服务端口。Platform 内解析前授权、解析后锁定 authority/revision 与提交事务继续内聚。模型路由独立登记和撤销，不能透明替换旧绑定。默认知识库继续直接运行现有 workflow，不增加外层模型规划。

## 验证记录

基线和结果保存于 `reports/rust-plugin-v1/`。决策记录见 [rust-plugin-decisions.tsv](rust-plugin-decisions.tsv)。最终工作包映射、结果、保留失败和边界见 [验收记录](rust-plugin-acceptance.md)，同机对照见 [性能报告](rust-plugin-performance.md)。

Python 3.12/3.13 最终正式 checker 各通过 455 项 Python 合同、65 项 Rust 测试、24 个评分器控制样本及受控进程、HTTP/PostgreSQL/parser 集成。默认业务冻结回放 48/48 通过，真实 PostgreSQL 恢复检查各 16 项通过。新增远端模型、Embedding、judge 调用为零。

交付按依赖形成五个 PR：R0 基线、R1–R6 运行时、E0 进程插件、B1/B7 模型与通用 Agent、B0/B2–B6/B8/C0–C2 知识库根组合。B7 与 B8 分开提交。新默认入口属于最后一个分支；`main` 尚未切换。旧计划文档保持原样，实际状态以验收记录和 PR 为准。

## Attention

reviewed by Codex（独立代理，与实施者同模型族）

跨模型族审计因平台 sealed-task 限制未能执行。已完成同模型族独立复核，不能表述为跨模型族审核通过。未验证范围与保留失败详见验收记录。
