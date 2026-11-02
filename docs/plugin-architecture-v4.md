# 插件架构第四轮实施

本轮依据当前会话授权实施。新配置、公共合同与业务数据格式不提供历史兼容、字段回退或自动升级。历史验收产物不参与产品运行。应用数据重置属于显式初始化操作，解析或连接失败不能触发清空。

## 目标边界

Rust 负责装配和生命周期，语言执行环境负责执行动作。插件通过版本化合同和具名出口连接；应用配置将消费者的本地依赖名绑定到提供者实例及其出口。合同 SDK 不必产生运行实例。存储依赖最终合并到统一 requires。

知识库用户可见能力为应用入口、工作流、上下文、会话、认证授权、模型、知识库数据与检索、存储。内部适配器、策略、资源转接和工具包装不单独形成产品插件。

上下文管理通过会话历史合同和授权检索合同获取信息，装配最终模型请求；工作流决定何时调用上下文、模型和是否继续执行。会话拥有完整历史和请求状态，知识库拥有原文、索引和证据，上下文拥有摘要和装配结果。插件读取其他领域数据必须经过领域合同，不读取其私有数据库表。

## 已实现：Rust 合同装配

`plugin-runtime::composition` 定义 Application、Definition、Contract、Requirement、Target 和 Plan。主宿主管道和嵌入 ABI 均可通过既有 dispatcher 调用 `runtime_compose`：

```json
{
  "version": 1,
  "protocol": "plugin-runtime/1",
  "command": "runtime_compose",
  "application": {
    "version": 1,
    "plugins": {
      "api": {"implementation": "example.http", "version": "1", "config": {}},
      "database": {"implementation": "example.database", "version": "1", "config": {}}
    },
    "bindings": {
      "api": {"records": {"plugin": "database", "export": "documents"}}
    },
    "entrypoint": {"plugin": "api", "export": "server"}
  },
  "catalog": [
    {
      "implementation": "example.http", "version": "1",
      "command": ["python", "app.py"],
      "provides": {"server": {"id": "application.run", "major": 1}},
      "requires": {"records": {"contract": {"id": "storage.document", "major": 1}}}
    },
    {
      "implementation": "example.database", "version": "1",
      "command": ["node", "database.mjs"],
      "provides": {"documents": {"id": "storage.document", "major": 1}},
      "requires": {}
    }
  ]
}
```

上述命令仅编译，不启动示例程序。catalog 是调用方提供的可信定义清单，目前不包含包安装与签名验证。插件版本精确匹配；合同按 ID 和主版本精确匹配，提供能力必须覆盖需求。每个依赖当前只绑定一个出口，optional 允许不绑定；显式提供的不兼容绑定即使 optional 也拒绝。

缺失绑定、未知实例、未声明依赖、未知出口、重复实现版本、错误入口合同和循环在创建实例之前拒绝。入口必须实现 application.run/v1。运行服务标识包含实例与出口，允许同一合同的多个提供者共存。编译结果保留本地别名到注册服务名的映射，执行 argv 不经过 shell。

Plan.plugins 是现有生命周期协调器可以接受的内部 Manifest 列表；Plan.order 为启动顺序。内部初始化 config 信封包含 configuration、bindings、command。`plugin_contracts.ContractApplication` 执行这一计划，`ContractContext` 将 provide/require 的本地名字映射为注册身份。进程消费者回调仅允许访问声明的依赖，继承父调用的租约和剩余截止时间。

新装配没有 Python 存储或模型知识，也没有新增生命周期状态机。runtime_compose 是无外部效果的纯编译入口，不表示应用已启动或业务已通过合同验证。

## 后续实施与完成标准

| 项目 | 当前状态 | 完成标准 |
| --- | --- | --- |
| Rust 合同匹配及绑定编译 | 已实现 | 错误组合零效果拒绝，正确组合生成现有生命周期依赖 |
| 配置 Schema 和方法合同执行 | Python 执行器已实现 | 逐插件配置、方法请求和返回值验证；各业务合同仍待发布 |
| 执行环境及 SDK 别名绑定 | 已验证 Python/Node 进程 | Rust 提供者合同 SDK 尚待补齐；运行时仍由 Rust 管理 |
| 主入口读取新应用配置 | 通用路径已实现 | contract_worker 无应用工厂启动；知识库正式启动配置仍待替换 |
| 统一存储依赖声明 | 已移除独立字段 | 类型化存储需求位于 requires；存储领域绑定编译仍在 Python，尚待接入跨语言合同 |
| 知识库八类能力收敛 | 待实施 | 内部类、回调、属性不跨插件边界 |
| 上下文主动获取历史和证据 | 待实施 | 历史 revision、来源、权限、最终模型请求完整可观测 |
| 数据重置 | 待实施 | 显式新建空业务空间，关联索引和缓存一起失效 |
| 全链路验收 | 待实施 | 不同语言提供者替换无需改上下游业务代码 |

既有细粒度应用尚未迁到新装配路径；保留它们是未完成的迁移工作，不是新架构的兼容承诺。当前不能宣称八类业务插件、任意语言业务替换或数据重置已经完成。

## 执行环境与 SDK

`kb_platform.contract_worker` 使用现有 plugin-host/1 监督协议。其 boot 文档包含 application、catalog、contracts、schemas、request、root，所有字段显式提供，不接受 Python factory。contracts 是版本化方法合同；每个方法包含 request/response JSON Schema。schemas 以 implementation/version 指定配置 Schema。root 是进程工作目录和 transport 日志归属目录。

每个方法接收一个 JSON 值并返回一个 JSON 值。SDK 拒绝无法序列化的对象、未声明的方法、非法输入和非法结果。配置与所有合同在创建插件之前校验，失败不挂载任何提供者。调用使用 Rust 注册身份和租约，关闭按原生命周期释放资源。stop 先排空，再请求取消活动调用，清理须等待实际执行结束。

`examples/contract-application/` 提供 Python 和 Node 的同合同进程消费者以及 Python 提供者。测试验证更换消费者语言不改变绑定，进程调用声明依赖，父运行时回收所有实例。示例是受控协议夹具，不代表知识库业务插件已经实现。

## 存储声明变更

PluginSpec 不再接受 storage_slots 字段。生产 catalog 和测试夹具统一把 StorageSlot 放入 requires。未编译的类型化需求直接交给 PluginHost 时明确返回 UNRESOLVED_PLUGIN_REQUIREMENTS；不回退到隐式存储。存储编译器将类型化需求替换为绑定服务依赖，并只在挂载上下文保存合同描述。

原先拒绝 storage_slots 中字符串的测试，现改为拒绝非字符串、非 StorageSlot 的值，以及两类需求之间的同名冲突。字符串在新的 requires 中表示合法的直接服务依赖。没有删除测试，保留合同类型、重复名称、缺失绑定、身份与权限校验。

## 验证

Rust composition/tests.rs 覆盖依赖顺序、同合同提供者替换、合同 ID/主版本/能力不符、缺失与未声明绑定、未知出口、可选依赖、循环、入口合同及协议确定性。统一执行 `uv run --no-sync python scripts/check_project.py`，不调用真实模型，不重放历史付费请求。
