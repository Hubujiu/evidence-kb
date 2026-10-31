# Rust 插件运行时

`rust/plugin-runtime` 决定实例、依赖、代次、注册、准入和清理结果。`agent-harness` 保留 loop 和 graph，并将 `runtime_*` 协议交给这一 coordinator。Python 保存服务对象、执行动作和回报结果。一个应用根只有一个生命周期权威。

来源、固定版本和实际参考轨迹见 [来源与基线](rust-plugin-provenance.md)。业务服务及资源所有权见 [业务插件](business-plugins.md)。

## 身份与状态

Rust 为每个 runtime、scope、instance、generation、operation、service registration、resource 和 lease 分配身份。对象地址不参与注册身份。重新注册同一个对象也产生新身份。`host.bind(name)` 保存具体注册身份，旧绑定不能透明转向新服务。

目标状态是 enabled、disabled、removed。实际实例状态如下。

| 状态 | 含义 |
| --- | --- |
| pending | 等待启用或依赖出现 |
| starting | 当前代次初始化尚未终结 |
| active | 服务已发布，可以准入新调用 |
| draining | 拒绝普通新调用，保留在途工作和旧依赖 |
| cleaning | Rust 已确认可清理，逐项派发资源释放 |
| cleanup_failed | 保留失败项、剩余资源及所需依赖 |
| failed | 初始化失败已清理，等待显式恢复 |
| disposed | 实例终态；重新加入使用新 instance |

宿主 `inspect()` 同时返回实例状态、目标与实际 revision、操作状态、受影响实例、失败原因、诊断和资源计数。宿主 `ready` 表示整体配置就绪，实例对应 `active`。

更新先排空受影响依赖子图，再清理消费者和提供者，最后激活新代次。无关插件继续服务。消费者清理期间只保留原已绑定依赖。旧初始化迟到后不能发布服务，其资源仍交回旧代次清理。失败初始化不会因为普通依赖刷新而无限重试。

## Python SDK

`PluginSpec` 声明实现键、版本、提供与依赖服务、并发方式，以及纯配置 schema/check。所有内置 catalog 在产生效果前校验配置。profile 的 `use` 只能引用可信 catalog 的键。

```python
from kb_platform.plugin_host import PluginHost, PluginSpec

def mount(ctx, config):
    counter = Counter(config['initial'])
    ctx.defer(counter.close)
    ctx.provide('counter', counter)

spec = PluginSpec('local.counter', ('counter',), (), mount,
    concurrency='serial', config_schema={
        'type': 'object', 'properties': {'initial': {'type': 'integer'}},
        'required': ['initial'], 'additionalProperties': False})
profile = {'version': 1, 'plugins': [
    {'id': 'counter', 'use': 'local.counter', 'config': {'initial': 0}}]}
with PluginHost(profile, [spec]) as host:
    result = host.call('counter', 'increment', 1)
```

`ctx.require()` 只访问声明依赖。`ctx.binding()` 返回注册、定义 scope、调用 scope 和 intercept 配置。`ctx.defer()` 在创建资源后立即登记 Rust 资源身份。初始化封存后不能继续 provide、defer 或 on。服务在自己的实际调用中创建新资源时使用 `ctx.effect(dispose)`，资源归属当前调用的实例和代次。

`host.service()` 用于装配。业务入口使用 `host.call()` 或固定身份的 `host.bind(name).call()`。默认 Python 插件声明 concurrent，领域事务、session revision 和业务租约继续约束数据并发。serial 插件拒绝重叠执行，同一已准入调用链的嵌套使用仍可收尾。

`binding.invoke(method, args=(), kwargs={}, timeout=seconds)` 设置截止时间。`host.current_call` 暴露实际 operation、lease、继承后的 deadline 和 `cancel_signal`。`host.cancel(operation)` 通知停止。deadline 定时通知也经过 Rust；通知和真实结束分别记录。同步 Python 函数必须合作检查信号，运行时不会强行中断函数。即使调用超过 deadline，函数实际返回前也保留其资源和租约。

`host.drain()` 停止普通新准入。`host.close()` 申请移除，忙碌时返回 `scope_busy`，生命周期回调中同步等待同一执行器返回 `PLUGIN_LIFECYCLE_BUSY`。清理异常保留为 exception group。`host.retry(plugin_id)` 和再次 close 是显式重试，只执行未成功清理的部分。

## 作用域和事件

`host.scope(profile, isolated=(), intercept={})` 创建同一根下的 scope。默认借用可见父服务。覆盖父服务必须写 `overrides`。isolated 中的服务在该边界隐藏，不能用已知 registration 绕过隐藏或覆盖。intercept 的服务配置从父到子递归合并，绑定同时保留定义处和调用处身份。子 scope 只清理自有资源。

普通构造严格检查缺失依赖。`PluginHost(..., dynamic=True)` 允许缺依赖实例停留 pending，后续配置可以补齐。循环和重复提供者在执行前拒绝。

`ctx.on(event, listener, priority=0)` 创建 Rust 所有的监听资源。相同优先级先父 scope 后子 scope，同一 scope 内按实际资源注册顺序执行。卸载撤销对应监听器。分发模式与固定参考轨迹对照。

| 模式 | 合同 |
| --- | --- |
| emit | 同步观察通知，输入元数据不可变；收集异常、记录诊断并继续交付 |
| parallel | 并行等待所有监听器，成功返回结果序列；异常全部记录并通过 exception group 返回 |
| serial | 顺序执行，遇到非 None 且非 False 的结果终止 |
| bail | 同步按相同终止谓词分发；数值 0 和空字符串是结果 |
| waterfall | `(value, next)` 中间件组合；重复 next 明确失败 |

Python 同步通知等待监听器完成，不承诺 JavaScript Promise 调度时序。parallel 任务各持有原已准入的 owner 租约，排空期间仍可完成合法嵌套调用。scope 表达可见性和所有权，不是恶意插件沙箱。

## 配置操作

`host.apply(profile)` 在任何卸载前验证条目、catalog、插件配置 schema、纯配置约束和依赖图。`host.update(id, enabled=False)` 停用实例；再次启用产生新代次。更换 `use` 即使服务签名和版本相同也会替换实现。重复 apply 相同配置不会重复挂载。

开始外部动作后不提供完整回滚。`desired_revision`、`observed_revision`、`operation.status` 和 `affected_instances` 如实显示未完成、失败和受阻状态。恢复旧配置也是新操作，不能撤销已经发送的请求。

所有正式入口通过根 profile 选择能力。API、delivery 和运维构造见 [插件配置](plugin-harness.md)。默认知识库直接执行现有图，不增加模型规划层；会话、预算、物理账本和 UNKNOWN 恢复仍由既有实现决定。

## 独立进程插件

E0 transport 使用带版本和调用身份的 JSON Lines，沿用 V1 实例、代次、deadline、取消和资源规则。以下示例插件没有编译进宿主。

```powershell
uv run --no-sync python -m scripts.plugin_manager --root .local/plugins install examples/process-echo
uv run --no-sync python -m scripts.plugin_manager --root .local/plugins list
$env:KB_PROCESS_PLUGINS_ROOT = (Resolve-Path .local/plugins).Path
```

在 profile 中选择 `{"id":"echo","use":"process.echo"}`。根 catalog 读取已安装、显式选中的包版本。安装记录保存来源、版本、文件哈希和内容地址；装载前重新核验内容。再次安装同名新版只选择新版，旧包和记录保持可读。

握手核对协议、能力和实际 Python 环境信息。manifest 可用绝对路径 `python` 选择预先准备的独立 Python 环境，省略时使用宿主解释器；安装器不自动下载安装依赖。入口作为包加载，辅助模块使用相对导入，宿主搜索路径不改变。业务信封携带 Rust operation、generation、lease 和 deadline。取消发送目标调用的独立通知，等待插件实际结束；超出停止等待期才终止进程并等待退出。`host.update('echo', enabled=False)` 停用当前实例。安装包不因此删除。

每次 transport 请求有唯一关联身份，记录 PREPARED、SENT 和终结结果。发送后崩溃或结果无法确认保留 UNKNOWN，没有自动重发。该 transport 记录不替代模型物理请求账本，也不把 UNKNOWN 费用当作零。重复相同响应在保留窗口内幂等，冲突响应使 transport 失败。外部进程退出时 Rust 撤销新准入，依赖方看到准确停用状态。

进程插件是可信本地包，可隔离崩溃和依赖环境。它不是恶意代码权限沙箱。Wasm 的不可信或可移植需求尚未确认，按计划保留为条件扩展；不支持原生动态库热卸载或全部 npm 插件兼容。

## 协议与验收

既有 C ABI 封装保持版本 1，生命周期命令另外要求 `protocol='plugin-runtime/1'`。submit/poll/complete/inspect 均为短调用，Rust 不反向调用 Python，状态锁和对象表锁均不跨用户代码。

同一 operation 的相同 complete 在确认前幂等，冲突回报保留原结果并记录诊断。Python 收到完成确认后发送 ack，明确退休重复回报历史；退休后旧 operation 返回 unknown，不能重新执行。disposed 根通过 collect 回收状态，Python 只保存 Rust 返回的终态快照。instance 和 registration 身份永不复用。

```powershell
uv run --no-sync python scripts/check_project.py --output reports/rust-plugin-v1/new-check
$env:KB_INTEGRATION_DSN = '<专用 rust_plugin_acceptance_ 数据库 DSN>'
uv run --no-sync python scripts/check_project.py --local-integration --output reports/rust-plugin-v1/new-integration-check
```

默认门禁禁止普通合同联网和启动子进程。checker 在单独受控 fixture 中验证外部插件安装、握手、调用关联、取消、deadline、排空、崩溃和 UNKNOWN。显式 local-integration 另行验证真实 loopback HTTP、中间件、解析子进程和 PostgreSQL。性能不设共享 CI 时延门槛，使用冻结同机预算单独验收。
