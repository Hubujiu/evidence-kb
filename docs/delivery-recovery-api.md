# 交付实例的受限恢复接口

固定交付实例启动时，如果当前物理账本保留 STARTED 或 UNKNOWN 请求，服务以 `restricted_recovery` 模式装配原有应用。生成模型、Embedding 身份、冻结容量、前序账本与当前预留保持原样。启动、状态检查和就绪检查不调用模型，不核销未知预留，不探测提供商，也不自动重试。

使用原有 Bearer 身份访问 `GET /agent/runtime-status`。响应包含 `mode`、`recovery_required`、`recovery_reason`、`new_paid_requests_allowed`、`mutations_allowed` 和 `recovery_requires_restart`。预算字段仍来自实际账本；账本不可读时保留未知值与读取失败原因，不以零代替。提供商的独立权限阻断仍单独报告。

受限模式允许经过原有认证和当前授权复核的会话清单、会话读取、历史、导出、请求查询、已保存产物、知识库清单与原件/引用读取。未登记为安全读取的接口默认拒绝。所有 POST、PUT、PATCH 和 DELETE 均拒绝，包括使用 POST 的人工搜索、表格查询和 `llm_context` 检索。拒绝发生在业务处理之前，返回 HTTP 503：

```json
{
  "error": "DELIVERY_RECOVERY_REQUIRED",
  "mode": "restricted_recovery",
  "reason": "UNRESOLVED_PHYSICAL_REQUEST",
  "model_sent": false,
  "automatic_retry": false,
  "allowed_actions": ["inspect_request", "inspect_history", "export_session", "reconcile_ledger"]
}
```

这里的 `model_sent: false` 描述被拒绝的新操作；既有未决请求的发送结果仍须单独核查。未认证请求保留原有认证失败响应，不返回恢复事实。

`GET /agent/readiness` 将依赖读取结果保留在 `read_only_ready`，完整工作就绪状态 `ready` 为 false。`GET /health` 继续只报告进程存活。

模型客户端发送入口和物理 transport 同时硬阻断；绕过 HTTP 业务入口也不能发出新请求。runtime batch 的恢复账本还禁止 admission、reserve 和 finish。现有前序校验、封存、资源维度和预算身份校验保持生效；未显式选择恢复读取的其他账本调用方仍拒绝未决子账本。

恢复模式在本次进程生命周期内固定。运维人员须先依据实际请求与回执核对未决事项，再通过已有账本处理流程解决；该 API 不提供清零、核销或重发操作。核查结束后重启服务才会重新评估准入条件。预检命令 `scripts/delivery_server_app.py --check` 同样报告恢复模式，且不改写账本。

统一 `delivery_status_contracts.live_suite` 覆盖真实应用重启后的已保存会话读取/导出、认证失败、各种写操作拒绝、runtime batch 预留保留，以及外部核查完成后仍须重启的发送阻断。这些是合成组件与接口合同，新增模型、Embedding 和裁判调用均为零，不表示生产 RAG 答案质量已经验收。
