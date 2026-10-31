# 第四轮修复与合并顺序

原子 `reserve(require_settled=True)` 拒绝新请求时，未发送任务必须保持可重试。本轮补齐 #21 的 `UNRESOLVED_PHYSICAL_REQUEST` 分类，并沿所有分支传播。#14 在 Google 边界验证本请求没有 capture 和物理记录，再包装类型化 preflight 错误，使标准 ModelService、EmbeddingView、EmbeddingAdapter 路径保留同一原因。

回归使用真实 SQLite 和线程屏障固定交错：A 读到空闲，B 进入 STARTED，A 的原子预留被拒绝，B 正常 COMPLETE。合同调用真实 classifier 和 `_finish()`，验证 A 为 RETRY、attempt 退回、熔断不增加、零发送。现有 PostgreSQL 验收入口另验证实际任务表、attempt 表及 circuit 表，并在 B 完成后用新的 request_id 重试 A。已有 capture 或物理记录仍不能证明未发送。

正式合同位于 `kb_platform/rag_testing/ledger_race_contracts.py`、`embedding_admission_contracts.py` 和 `embedding_jobs_contracts.py`。PostgreSQL 验收继续使用 `kb_platform.rag_testing.postgres_operations`。组件合同与评分器控制分别报告，新增真实供应商模型、Embedding 和 judge 调用均为零。

## 分层整合

#14 先进入 #15，再逐层传播到 #20。#19 保留 Rust 模型路由、租约与审计实现，#20 保留根组合、重排与整轮预算实现。整合保留 #14 的模型配置示例、`core-models.json`、模型服务文档和恢复合同。`postgres_operations` 同时发现两组原有模型 live 合同，防止整合使旧行为失去验收入口。没有删除原测试。

推荐合并顺序如下，实际合并后继续核对 GitHub base 和 required 状态：

1. #1、#2、#3。
2. #21。
3. #14。
4. #15、#16、#17、#18、#19、#20。

采用保留历史的 merge commit 或快进，使后续分支能识别已整合的祖先。对依赖栈使用 squash 或 rebase merge 会改变祖先关系，需要重新核对差异和冲突。

本轮只更新候选分支和 Ready 状态，不向 main 合并。各 PR 的旧 CI 不代表本轮整合结果，判断以最终 HEAD 的新 CI 为准。最终 #20 同时包含三个 Action 升级，验证其与完整产品树的组合。

## 重跑门禁

修改后的每个候选分支运行统一入口：

```powershell
uv run --no-sync python scripts/check_project.py
```

最终整合树使用同一入口的 `--local-integration`，在独立 `rust_plugin_acceptance_*` 数据库中执行真实本地 HTTP、解析子进程和 PostgreSQL。物理提供者由受控响应代替，不调用远端模型。原生库来自该轮源码的独立 release 构建，合同加载该轮复制出的库。

先前专项证据包、失败前日志和本轮输出保留只读。没有运行 TLC，没有把合成并发合同当作生产 RAG 质量或全调度形式证明。
