# 检索失败与 continuation 清理失败的优先级

审阅基线：`f0ea7450b33b21022fbb61e91c1539d5a290c89b`。

## 已复现的问题

Agent 检索会先准备候选证据，在调用方重排，然后凭一次性 continuation 完成管道。重排失败时，`EvidenceClient.search_pipeline` 会请求取消 continuation；如果取消本身也失败，原实现把取消错误向上传播，覆盖原来的模型错误或取消信号。

这会改变调用方对结果的理解。例如，带物理请求身份和未知 usage 的 `PROVIDER_OUTCOME_UNKNOWN` 被普通 `PlatformError` 替代；依赖顶层错误的分类和恢复逻辑无法从该错误直接读取原始调用事实。`KeyboardInterrupt` 一类 `BaseException` 也会被替换成普通异常。Python 原有异常上下文仍可能包含主错误，但公开错误投影不自动遍历该上下文来恢复其事实。

## 修复规则

在主操作已经失败时，只尝试一次取消，并保留主异常对象和裸 `raise`。如果取消也失败，给主异常加上 `Retrieval continuation cancellation failed: <异常类>` 的诊断 note。note 使用异常类名，不拼接清理异常原始消息。

| 重排结果 | 取消结果 | 调用方观察 |
| --- | --- | --- |
| 模型错误 | 取消成功 | 原模型异常及其调用事实 |
| 模型错误 | 取消失败 | 原模型异常及其调用事实，附清理失败 note |
| `BaseException` 取消 | 取消成功 | 原取消对象 |
| `BaseException` 取消 | 取消失败 | 原取消对象，附清理失败 note |

该改动不会重试模型、检索或取消，也不退款、不把未知结果改成已知结果、不宣称清理已成功。成功重排后的正常完成路径保持原有协议。没有变更 API 格式或数据持久化格式。

## 回归证据

`KnowledgeCapabilityContracts.test_rerank_primary_failure_survives_cancellation_cleanup_failure` 使用实际 staged 检索生成器及公开 `EvidenceEndpoint → ServiceEndpoint`。受控的生成器 finalizer 注入清理失败，模型错误和取消信号均为合成输入。

同一合同在旧实现上有两个明确失败：清理失败时，原 UNKNOWN 异常或原 `KeyboardInterrupt` 被 `PlatformError('Service implementation failed')` 替换。两个清理成功对照通过。修复后四种组合全部通过，并核对：

- 主异常对象完全相同，canonical `error_details` 中的 code、物理请求身份和 usage 事实不变，UNKNOWN 仍被分类为 UNKNOWN。
- 查询、Embedding、重排和取消各执行一次；没有因失败自动重复。
- continuation 已按现有协议消费，再次提交同一 token 被拒绝；清理失败 note 存在，成功取消不制造 note。

knowledge capability、auth/cache continuation、evidence boundary、evidence wire 和 formal combination 五个合同模块共 50 个测试通过。Ruff 及整个登记表的直接 formal lint 通过。新增付费请求为 0，未进行远程模型或真实 HTTP 网络质量评测。

## 模型与完整检查的边界

现有 `EvidenceContinuation` 模型把不同 finish 结果折叠成资源处置动作；`ErrorProjection` 从已经选定的主错误开始。它们都没有证明主失败与清理失败的异常优先级。本次保持模型和历史输入清单不变，补充 client 和回归方法的源码绑定，并明确该项由实际合同验证。详见 [cache/evidence 模型说明](../formal/cache_evidence/README.md)。

异常 note 在任意后续插件或持久化边界的完整传播、失败 close 后外部资源的恢复、进程死亡和传输丢失不属于本次保证。保留主错误不等于解决这些不同生命周期问题。

两个必需入口均已执行：普通 `scripts/check_project.py` 与 `--formal-only` 均因本环境 owned process runner 在启动子命令前返回 `RuntimeError` 而 FAIL，与根审阅在未改动基线上观测的环境限制一致。未放宽门禁或把直接定向测试冒充完整验收；合并前仍需 CI 通过这两个入口。
