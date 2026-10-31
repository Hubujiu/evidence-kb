# Rust 主宿主改造

用户决定以 DSH 现有插件行为为基准，插件由安装者信任，不增加插件安全沙箱。Rust 通用核心继续唯一拥有生命周期，Python 保留业务对象和算法。跨语言接口用于执行与组合，不作为安全边界。

## 任务

- [x] Ground：核对现有 Rust/Python 调用、入口、回归合同及 DSH 参考版本。
- [x] Sketch：比较两个宿主实现方案，固定公共调用方式与对象所有权。
- [x] Agree：依据已授权目标选定方案，直接实施。
- [x] Implement：Rust 主宿主、语言无关执行协议、Python 执行环境、默认入口和确定性验收。
- [x] Scrap：实现可保留原有动作执行器与清理语义，无需推翻总体设计；修正 stop/close 和 EOF 时序。
- [x] Verify：执行统一检查及真实本地进程闭环，记录范围和剩余限制。

## 设计比较

比较标准为单一 Rust 生命周期权威、现有业务复用、跨语言可调用性、取消与退出语义、公共接口规模。

方案 A 将既有 `native_call` 改为 worker 到 Rust 主进程的调用，复用已验收的 Python 动作执行器；方案 B 让 Rust 主进程逐项驱动插件对象的 mount/dispose/invoke，并重写跨进程对象目录。选择 A，因为它在搬移生命周期状态所有权时保留原有资源登记、嵌套调用、完成与 ack 逻辑，协议面更小。吸收 B 的启动就绪、根回收和异常退出核验。

`plugin-host` 保持通用，dispatcher 由 `evidence-host` 注入；Python worker 的应用工厂支持 Agent 或非 Agent 服务。普通独立进程插件新增 command argv 与 requires 回调，依赖在原请求线程执行，避免丢失调用上下文和父租约。没有构建第二套插件生命周期图。

接入复核发现 `llm.complete()` 的类型化返回值无法直接通过 JSON 传输，因此补充 `complete_json()` 薄适配，复用原有 complete 和 CompletionEnvelope.to_dict。组合验收包含外部插件调用实际公共模型服务与物理账本，不仅验证合成服务名。

DSH 基准固定为 `ce7073d51254c26cd63b22a60835b8f948deb447`，2026-10-31 远端 HEAD 核对一致。文档、使用方式与具体边界见 [Rust 主宿主](rust-main-host.md)。保留单次 Agent 值状态 C ABI，不将其误称为第二个插件运行时；不提供 JavaScript 二进制兼容。

## 验证范围

优先使用现有合同和冻结回放。模型边界采用本地受控响应；本次不需要新增付费模型、Embedding 或裁判调用。保留会话事务、文档权限、最终请求采集及唯一物理费用账本。

首次完整检查 `reports/rust-main-host/20261031-check-01/` 保留 FAIL：新进程用例误放入离线合同，已迁移到既有进程集成，原 argv/工作目录断言保留；宿主测试启动时仍加载旧 EOF 实现，当前实现通过 40 次重复运行，并增加可以检出旧缺陷的确定性合同。测试期间新增合同文件导致发现集合变化，最终验收须使用稳定源码重新执行，不能复用首次部分 PASS。

最终统一检查 `reports/rust-main-host/20261031-check-04/summary.json` 为 PASS：架构、Rust 格式/Clippy/构建、68 项 Rust 测试、539 项 Python 合同、独立进程集成、13 项宿主集成和 24 项评分器控制全部通过。检查使用本轮独立构建副本，未放宽离线审计、超时或发现集合门槛。

Node 同协议集成另见 `reports/plugin-process-language/node-dependencies-20261031-1/result.json`，包括字面 argv、正常调用、依赖回调、截止时间、取消、崩溃与清理。Python 对应记录位于同目录的 `python-dependencies-20261031-1/`。关停重复验证记录在 `.local/host-review-repeat-01/`。这些本机报告不随公开源码发布。

新增真实提供者模型、Embedding、judge 调用均为 0；所有模型响应由受控本地 transport 提供。未执行生产数据库和企业业务金标验收，不据此宣称 RAG 质量或性能提升。交付入口和示例已迁移，原有 Python 嵌入方式仍供组件使用；单次 Agent 值状态 C ABI、同步模型接口和可信插件责任边界保持明确。
