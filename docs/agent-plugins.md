# Agent 插件与业务边界

profile、插件生命周期和服务扩展示例见 [插件宿主](plugin-harness.md)，原生状态机和构建方式见 [Rust 通用运行时](rust-agent-runtime.md)。默认 API 通过 profile 选择应用服务；模型、上下文、会话、工具和 loop 都有独立服务边界，不读取全局可变工具目录。

## 插件与工具的区别

`PluginSpec` 声明一个插件提供和依赖的服务，由宿主排序、启动和清理；`ToolPlugin` 是模型可调用的一个能力，包含参数 Schema、效果元数据和执行函数。一个插件可以注册工具目录、模型适配器、会话服务或 loop，工具只是其中一种能力。

`plugin_host.PluginHost` 承载 Python 实现，Rust `agent-harness` 决定依赖图、服务归属和作用域生命周期。`ctx.require()` 解析声明的依赖；`ctx.provide()` 注册服务；`ctx.defer()` 注册插件自己拥有的资源清理；`ctx.on()` 注册固定事件观察者。子作用域借用父服务，覆盖需要显式 `overrides`；关闭只处理自身拥有的子树。

## 通用工具

```python
from kb_platform.agent_plugins import ToolPlugin, ToolResult
from kb_platform.agent_tool_loop import ToolAgent

def double(context, arguments):
    return ToolResult({'value': arguments['value'] * 2})

tool = ToolPlugin(
    'double', 'Double an integer.',
    {'type': 'object', 'properties': {'value': {'type': 'integer'}},
     'required': ['value'], 'additionalProperties': False},
    double, version='1', retry_safe=True,
)
agent = ToolAgent(decide, [tool])
```

`ToolAgent` 是 profile 宿主的便捷入口，默认使用 Rust `model-tools` loop。每次运行启动独立宿主并结束清理；`decide` 通过 `harness.model.callable` 适配，接受模型历史和 manifest，返回 `ToolCall(name, arguments)` 或 `Answer(value)`。通用 `ToolContext` 只包含宿主提供的身份、会话、轮次和调用标识；`ToolResult` 只包含结构化数据，不带知识库字段。

参数在执行边界校验。工具目录复制 Schema，拒绝重复名字。`retry_safe` 不能与 `mutates_data` 或 `incurs_cost` 同时成立；该声明不会让内核自动重试。`direct_return` 由可信插件定义决定是否直接交付结果，模型不能通过输出字段开启它。

默认工具注册服务为 `harness.tools`。每次请求绑定的能力通过 `ToolPlugins.bind()` 创建独立目录，不能修改正在使用的全局目录。更换会话、上下文或 loop 可以传入 `ToolAgent(..., profile=..., extra_plugins=...)`，替换相应 catalog 键，无需修改模型工具算法。

## 知识库扩展

`KnowledgeService` 的内部规划工具使用 `KnowledgeToolContext` 与 `KnowledgeToolResult`，在通用接口之外增加授权范围、来源策略和证据。知识库专属规划 Schema、保留名称与提示词投影属于 `KnowledgeToolPlugins`。

```python
from kb_platform.agent_plugins import ToolPlugin
from kb_platform.knowledge_plugin_tools import KnowledgeToolResult

def lookup(context, arguments):
    evidence = gateway.search(context.actor, context.scope_id,
                              arguments['query'], context.source_policy)
    return KnowledgeToolResult({'count': len(evidence)}, evidence)

plugin = ToolPlugin('lookup_rule', 'Read authorized original rules.',
    {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 1}},
     'required': ['query'], 'additionalProperties': False}, lookup)
```

`KnowledgeService(..., tool_plugins=[plugin])` 安装知识库内部工具。返回的证据仍经过来源与原文核验，插件不能直接把自造内容变成可引用证据。

要把整条 RAG 能力提供给通用 Agent，使用 `knowledge_tool.query_tool(service, session_id)` 注册 `kb_query`，无需让通用核心依赖内部知识工具、证据 Schema 或数据库。该工具继承父执行边界，并直接交付服务已经核验的结果。

默认 HTTP 应用通过 `knowledge_application.KnowledgeTools`（服务名 `knowledge.tools`）绑定 `kb_query`，保留 HTTP 调用者的原始 request ID、expected revision 和来源/工作流约束，再交给 `harness.loop` 的 `single-tool` 模式。该路径没有多余的外层模型决策和答案改写；模型、上下文、持久存储、授权网关、工具执行器及语义检索都由 `knowledge_profile.py` 注入。

## 提供者与持久化

`agent_provider.ModelProvider` 归一化模型结果、精确缓存身份与物理尝试记录；`agent_luna.LunaModelProvider` 适配现有 Luna 客户端。`AgentModel` 不导入具体 HTTP/SDK 提供者。新增提供者必须继续接入唯一的实际请求计量与费用账本，不能自建隐藏重试链。

Rust 拥有插件生命周期和临时 loop 状态，消息存储通过会话服务接口完成。默认 `harness.sessions.memory` 仅保留单次运行的临时消息并在结束后释放。知识库会话与工具产物仍由 `AgentStore` 保存，现有范围授权、租约和哈希检查继续执行。通用内核没有隐式持久化、checkpoint 重放或失败自动恢复。

`host.call()` 对服务调用取得运行租约。`drain()` 停止接收新调用；`close()` 在所属子树无活动租约时按启动相反顺序清理。忙碌和清理失败会明确报错，不能把卸载理解为取消、撤回或重复调用工具。同步 Python 效果仍依赖协作取消。
