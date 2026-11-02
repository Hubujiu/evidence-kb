# 交付预算状态查询

`GET /agent/runtime-status` 对已配置的交付服务返回当前预算与提供者通道的只读状态。查询不会发送模型或向量请求，不创建物理请求，不清零旧费用，也不把未知预留当作实际零用量。状态是查询时的事实；真正发送前仍由原有预算与提供者准入流程检查。

## 读取失败的公共结果

当前预算账本不可读取时，接口仍返回 HTTP 200 和可解释的 JSON：`status`、`budget_status` 为 `BLOCKED`，`blocked_reason` 为 `BUDGET_READ_UNAVAILABLE`，`usage_available` 为 `false`。当前和累计用量、余额、未决请求数、未知用量请求数均为 `null`。已知配置 `caps` 保留；错误信息不包含底层异常文本、路径、SQL 或凭证。

```json
{
  "configured": true,
  "status": "BLOCKED",
  "budget_status": "BLOCKED",
  "blocked_reason": "BUDGET_READ_UNAVAILABLE",
  "usage_available": false,
  "cumulative_usage_available": false,
  "effective_usage_or_reserved": null,
  "remaining": null,
  "current_pending_or_unknown": null,
  "pending_or_unknown": null,
  "unknown_usage_requests": null,
  "cumulative_unknown_usage_requests": null,
  "cumulative_effective_usage_or_reserved": null,
  "model_calls_from_status": 0
}
```

`null` 表示当前不能确定。数值 `0` 只表示已读取并核实为零。控制台分别显示余额未知、当前账本不可读或历史账本不可完整核验，不显示为已用尽或没有未决请求。

## 本批次与历史事实

`BudgetLedger.status_snapshot()` 与 `LinkedBudgetLedger.status_snapshot()` 提供相同的公开读取能力。交付接口只消费这个能力，不检查具体账本类型，也不访问内部子账本。

| 字段 | 含义 |
| --- | --- |
| `usage_available` | 当前预算或当前批次用量可读取。 |
| `effective_usage_or_reserved`、`remaining` | 当前预算或当前批次的已用或保留额度、各维度剩余额度；不可读时为 `null`。 |
| `current_pending_or_unknown` | 当前预算或当前批次的 `STARTED` / `UNKNOWN` 请求数。 |
| `cumulative_usage_available` | 当前与全部所需历史账本的累计用量可核验。 |
| `pending_or_unknown` | 全部所需账本的未决请求数；历史不可核验时为 `null`，不退回当前批次的数字。 |
| `reviewed_unknown_requests` | 已验证人工审阅记录覆盖的历史未知请求数；审阅或历史不能核验时为 `null`。普通单账本没有前序审阅，值为 `0`。 |

父账本不可读取时，状态为 `BLOCKED / PREDECESSOR_LEDGER_UNAVAILABLE`。当前批次仍可读取的用量、余额、未知预留和 `current_pending_or_unknown` 会保留；累计用量、累计未决数及审阅数量保持 `null`。已确认父账本偏离冻结版本时，原因是 `PREDECESSOR_LEDGER_CHANGED`。当前批次也不可读取时，优先报告 `BUDGET_READ_UNAVAILABLE`。

经过既有明确审阅而获准继续的历史 UNKNOWN 仍保留原状态与预留。状态读取沿用原有审阅规则：累计未决数可以大于零，`reviewed_unknown_requests` 说明其中已获准继续的部分；不会把审阅当作提供者已确认完成，也不会重新发送旧请求。

## 验收范围

`kb_platform/rag_testing/delivery_status_contracts.py` 使用真实 SQLite 账本验证普通读取故障、当前批次读取故障、父账本不可读与变化、已知零值和额度用尽、已审阅 UNKNOWN 的保留。故障通过临时隐藏测试表或测试账本注入，恢复后检查原有费用和记录。

同模块 `live_suite()` 通过真实 FastAPI 路由和 ASGI HTTP 请求验证普通及批次状态返回 JSON，并确认零模型调用、零向量调用和零新增物理请求记录。其余应用对象为合成夹具；这不是外部提供者或生产 RAG 质量验收。该组必跑用例已接入 `foundation_integration`，离线合同由统一检查器自动发现。

完整检查沿用 `uv run --no-sync python scripts/check_project.py`，不改变原有门槛和超时。
