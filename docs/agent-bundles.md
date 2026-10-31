# Agent 组合包与基础服务边界

产品通过 profile 选择显式注册的组合包，再由插件宿主装配服务。组合包是配置集合，不是另一套执行引擎。当前 HTTP 产品默认加载 `knowledge`；`core` 仅由调用方显式创建，不会随知识库服务启动，也没有新增 HTTP 路由。

## 两种现有组合

| 职责 | `core`：通用持久 Agent | `knowledge`：现有知识库产品 |
| --- | --- | --- |
| 入口 | `harness_profile.create_agent_application()` | `knowledge_profile.create_knowledge_application()` |
| profile | `config/agent-profiles/core.json` | `config/agent-profiles/knowledge.json` |
| 组合包 | `config/agent-bundles/core.json` | `config/agent-bundles/knowledge.json` |
| 执行策略 | Rust `model-tools` loop | `knowledge.driver.graph` 直接进入知识库流程，Rust 推进图节点 |
| 持久化 | `harness.store.sqlite` → `session.store` | 同一个通用 SQLite 实现 |
| 会话策略 | `harness.sessions.durable` → `sessions` | `knowledge.sessions` → `knowledge.store` 业务适配器 |
| 上下文 | `harness.context.history`，按完整轮次保留历史 | 既有知识库历史、证据和 checkpoint 组装 |
| 模型 | 调用方注入 callable 或结构化模型适配器 | 既有 Luna 提供者、模型服务与费用账本 |

知识库默认路径不再创建外层 `single-tool` loop 和临时会话。可信 `kb_query` 绑定仍保留原始 actor、session、request ID、expected revision、来源策略和用户约束；图驱动直接执行该绑定。没有增加外层模型决策，也不改写已核验答案。`ToolAgent` 和 `single-tool` 用于显式选择的单工具调用。

## 无网络的最小通用 Agent

从仓库根目录运行，先按 [Rust 运行时](rust-agent-runtime.md#构建与验收) 构建原生库并安装 Python 依赖。示例只调用本地函数，持久数据在退出临时目录时删除：

```python
from tempfile import TemporaryDirectory

from kb_platform.agent_decisions import Answer
from kb_platform.harness_profile import create_agent_application
from kb_platform.session_identity import Actor


def decide(messages, tools):
    return Answer({"text": "本地回答", "user_messages": sum(
        message["role"] == "user" for message in messages
    )})


actor = Actor("example-tenant", "example-user")
with TemporaryDirectory() as root:
    settings = {"AGENT_CORE_ROOT": root}
    app, host = create_agent_application(decide=decide, environment=settings)
    with host:
        session = app.create(actor)
        first = app.turn(actor, session["session_id"], message="第一轮",
                         request_id="request-1", expected_revision=session["revision"])
        print(first["result"])  # user_messages 为 1

    # 使用相同存储重新装配；历史恢复不依赖原宿主的内存。
    app, host = create_agent_application(decide=decide, environment=settings)
    with host:
        restored = app.get(actor, session["session_id"])
        second = app.turn(actor, restored["session_id"], message="第二轮",
                          request_id="request-2", expected_revision=restored["revision"])
        print(second["result"])  # user_messages 为 2
```

`turn()` 返回包含 `status`、`result`、`session_id` 和 `revision` 的完成响应。身份来自可信宿主；通用 `Actor` 不是认证中间件。默认组合必须显式提供 `decide` 或满足决策接口的模型对象，没有内置网络模型、密钥发现或静默回退。

`harness.model.structured` 可把已有计量的结构化模型对象适配成决策服务。选用时将 core profile 的 `model` 条目改为该 catalog 键，并通过 `create_agent_application(model=...)` 注入现有对象。它通过 JSON Schema 表达答案或工具选择，不宣称使用提供者原生 tool-calling 协议。辅助插件可以调用该适配器的 `complete()`，复用同一个模型、提供者与账本；callable 接口不自动替调用方建立费用计量。

## 组合与替换规则

profile 和组合包使用相同文档结构。例如仅替换存储目录：

```json
{
  "version": 1,
  "bundles": ["core"],
  "plugins": [
    {"id": "store", "config": {"root": ".local/my-agent"}}
  ]
}
```

`plugin_profiles.expand_profile()` 在挂载前展开组合包。组合包名必须存在于调用方提供的 catalog；配置不能下载包、动态 import 模块或执行表达式。嵌套组合可用，但循环、重复选择和组合之间冲突的插件 ID 会被拒绝。

当前 profile 的插件条目按 `id` 覆盖组合包条目；省略的条目字段沿用原值，显式提供的 `config` 替换整个配置对象，不做深层合并。`disabled: true` 可以排除条目，但展开后的所有消费者仍须满足依赖。替换 `use` 选择的是已注册 `PluginSpec`；新实现须通过可信 catalog/`extra_plugins` 提供。

知识库使用 `harness.store.sqlite` 后端和 `knowledge.sessions` 适配器。`KB_AGENT_ROOT` 覆盖后端目录；对外服务名为 `knowledge.store`。

同名服务不意味着任意实现可以互换：调用方法、请求/结果合同和状态格式都必须兼容。通用应用调用 `loop.run()`；知识库应用使用可信工具绑定的 `loop.invoke()`。图程序由可信 Python 代码提供节点和路由，当前知识库通过 `knowledge.workflow.graph` 注入；仅写一份 JSON 图不会自动产生业务节点。

## 会话、状态和上下文的所有权

`session_store.SessionStore` 唯一实现现有 SQLite 表和事务：会话归属、revision、单写者租约、请求指纹、历史事件和制品。最终 assistant 事件、状态与响应在同一事务提交。知识库状态的初始化与失败响应规则属于 `knowledge_state`；`KnowledgeSessions` 适配这些规则。

通用会话使用 `agent.generic/1` 命名空间，拒绝把其他应用的会话当成普通聊天读取。失败轮内已经持久化、配对完整的工具调用和结果仍进入后续模型历史，并附上来源为 runtime 的失败或中断状态，避免模型遗忘已经发生的工具效果。原审计记录保留。

若历史存在已记录调用但没有结果的工具，新请求会被 `SESSION_OUTCOME_UNKNOWN`（409）阻止，不能把可能已经执行的效果当作未发生而重做。初次准入检查发现该情况时不写入新轮记录，也不调用模型或工具；已完成请求仍可按原指纹返回原响应，包括已完成的失败响应。当前没有解除该未知状态或恢复中断节点的接口，需先核对原操作结果；另建会话也不会消除原效果。原生 loop/graph 句柄不是持久 checkpoint。

`harness.context.history` 从完整历史构造本次输入，`harness.compaction.recent` 只选择连续的完整轮次后缀，保留工具调用/结果配对。它不调用摘要模型，不改写持久历史。工具定义及结构化模型包装计入相应输入估算；目前仍是保守估算，不能宣称精确 token 保证。知识库现有证据权限复核和最终交付检查继续由知识库模块负责。

## 参考 DSH 的边界与本项目差异

对照固定提交 [DSH f3803c9](https://github.com/deepseek-ai/deepseek-harness/tree/f3803c96974ad4a94195fb97e5c637e9c2a3afe3)：

- DSH 分离 [Session 日志](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/core/session/src/index.ts)、[持久化服务合同](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/session/session-persistence/src/index.ts)和具体后端。本项目抽出通用事务存储与业务会话适配，保留现有 SQLite 原子提交，没有迁成 DSH 的 JSONL 事件溯源系统。
- DSH 将 [压缩服务](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/compaction/compaction/src/index.ts)与实现分开。本项目将通用上下文组装和完整轮次保留策略分开，未增加模型摘要或压缩状态重放。
- DSH 的 [摘要插件](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/compaction/compaction-basic/src/summarizer.ts)可独立调用公用 `llm` 服务。本项目也允许辅助插件调用共享结构化模型服务，不强制每个辅助请求经过主 Agent loop。
- 本项目 bundle 是显式配置展开，不是 DSH preset 的完整兼容层。没有第三方插件市场、运行中 preset 热更新、多 Agent 调度、Wasm 或进程沙箱。

Rust 负责运行控制、通用 loop 与图推进；Python 继续实现模型、工具、上下文、数据库和知识库算法。图中的业务路由选择仍由 Python 提供，Rust 检查声明边、当前节点与结果序号。可信插件的工程边界不构成恶意代码隔离，也不代表全链路性能提升。

## 验证

统一入口仍为 `uv run --no-sync python scripts/check_project.py`。新增合同与既有合同共用 `kb_platform/rag_testing/`，覆盖不加载知识库的持久 Agent、上下文配对、事务和幂等、组合展开及图推进，并保留知识库回归。离线模型和数据夹具证明组件行为；不把它们当作线上 RAG 质量、真实提供者兼容性或性能成绩。
