# Rust 插件迁移最终性能验收

> 历史性能记录：本文 final-04 测量冻结于 `5d18294`，属于审查前 `105d664` 所指实现的历史证据。后续八项缺陷修复已改变生产源码。原始数字和报告保持只读，不能作为当前修复版的性能 PASS。修复版结果见 [审查修复性能验收](rust-plugin-review-performance.md)。

九项既有工作量均通过冻结绝对预算。五轮 baseline/current 交替测量中，各项当前进程 p95 范围均低于 baseline，范围未重叠。新原生核心和完整离线业务没有旧版同等工作量对照，单独报告。

测量使用同机、同一 Python、release 制品与冻结源码副本。每侧五个独立进程。hot call 每进程 1000 个样本，scope 和 single-tool 各 100 个，预热各 20 次。失败样本保留在分母，本轮错误为零。预检系统 CPU 忙碌率为 3.85%，逻辑 CPU 数为 24。

## 既有工作量与固定预算

单位为微秒。p95 是五个进程 p95 的中位数，范围为这五个值的最小值和最大值。验收按当前五个进程 p95 的最大值核对原有绝对预算，预算未改变。

| 工作量 | 规模 | 旧 p95 | 旧 p95 范围 | 当前 p95 | 当前 p95 范围 | 固定预算 |
| --- | ---: | ---: | --- | ---: | --- | ---: |
| hot_call_last_service | 1 | 578.6 | 559.6 至 608.7 | 68.5 | 61.0 至 156.1 | 953.4 |
| empty_scope_create_inherited_call_close | 1 | 1673.7 | 1612.4 至 1790.6 | 273.5 | 252.5 至 356.3 | 1997.6 |
| hot_call_last_service | 32 | 589.3 | 583.0 至 602.8 | 69.2 | 65.6 至 76.5 | 1073.3 |
| empty_scope_create_inherited_call_close | 32 | 1775.6 | 1620.7 至 1873.9 | 325.8 | 294.2 至 364.6 | 2348.3 |
| hot_call_last_service | 128 | 578.7 | 558.4 至 610.3 | 85.1 | 78.9 至 90.2 | 758.2 |
| empty_scope_create_inherited_call_close | 128 | 1657.8 | 1642.8 至 1755.8 | 414.7 | 382.7 至 433.0 | 2091.8 |
| hot_call_last_service | 256 | 579.5 | 557.0 至 600.0 | 107.6 | 100.6 至 182.5 | 614.2 |
| empty_scope_create_inherited_call_close | 256 | 1707.2 | 1635.0 至 1969.9 | 567.6 | 556.4 至 733.9 | 1989.8 |
| single_tool_turn | 4 | 3927.3 | 3792.1 至 4135.1 | 1512.2 | 1448.3 至 1701.7 | 4469.3 |

## 新增工作量

以下结果只有当前实现。bridge 并发工作量以整批为时延单位，整批分别包含 1、4、16 次调用。完整业务包含冷组合、会话创建、真实 SQLite 提交、默认知识库图和清理。每轮业务执行四次合成模型调用并验证最终可见原文与持久 revision，远端调用为零。

| 工作量 | 并发 | p95 中位数 | p95 范围 |
| --- | ---: | ---: | --- |
| new_bridge_idle_stop_and_collect | 1 | 411.5 | 384.3 至 477.8 |
| new_bridge_cold_create_call_dispose_collect | 1 | 674.1 | 638.5 至 757.5 |
| new_bridge_dependency_replace_and_invoke | 1 | 538.5 | 482.5 至 606.6 |
| new_bridge_event_emit | 1 | 126.7 | 119.8 至 193.1 |
| new_bridge_event_parallel | 1 | 973.9 | 897.3 至 1074.9 |
| new_bridge_concurrent_calls | 1 | 201.7 | 141.7 至 243.3 |
| new_bridge_concurrent_calls | 4 | 713.1 | 671.4 至 839.7 |
| new_bridge_concurrent_calls | 16 | 2760.3 | 2586.3 至 3754.1 |
| new_offline_business_default_graph_with_durable_commit | 1 | 963674.8 | 522105.2 至 1247049.4 |

原生核心由五个新进程各执行32项组合，包含目录规模 1、32、128、256，冷生命周期、调用、scope、依赖替换、监听器和并发。所有回调计数符合预期，所有终结资源计数为零。完整结果见 analysis.json 的 native 列表和 measurements/native-process-*.json。

## 限制因素与内存

独立 profiler 在测量后执行，没有混入时延样本。其结果是定位成本的诊断，不能作为配对时延结论。

current profiler 总时间 1.499 秒，相关操作：native_call 28384 次，内部 0.412 秒，累计 0.804 秒；<built-in method nt._getfinalpathname> 246 次，内部 0.021 秒，累计 0.021 秒。

business profiler 总时间 3.840 秒，相关操作：native_call 2171 次，内部 0.039 秒，累计 0.074 秒；_getfinalpathname_nonstrict 10 次，内部 0.000 秒，累计 0.002 秒；<built-in method nt._getfinalpathname> 70 次，内部 0.005 秒，累计 0.005 秒；<method 'close' of 'sqlite3.Connection' objects> 87 次，内部 0.313 秒，累计 0.313 秒；<method 'commit' of 'sqlite3.Connection' objects> 87 次，内部 0.711 秒，累计 0.711 秒。

桥接成本主要在原生边界和 JSON 编解码。动态库路径已按配置缓存；Windows 路径解析只在初次加载等设置操作中发生。完整业务包含多次 SQLite 持久提交，文件系统等待影响其变化范围。微测量下降比例不能套用到完整业务或真实模型服务。

每个新进程预热 20 次后重复创建、调用、关闭并收集 200 个根运行时，每 20 次完成后读取 Windows 当前 PrivateUsage、WorkingSetSize、句柄数和 Python 线程数。以下为进程观测，不以累计峰值推算单个 Rust 内核内存。

| 进程 | PrivateUsage 首值 | 末值 | 增量 | 范围 | 句柄数 | Python 线程数 |
| ---: | ---: | ---: | ---: | --- | --- | --- |
| 0 | 65732608 | 65732608 | 0 | 65732608 至 65732608 | [199] | [1] |
| 1 | 64319488 | 64319488 | 0 | 64319488 至 64319488 | [199] | [1] |
| 2 | 64208896 | 64208896 | 0 | 64208896 至 64208896 | [199] | [1] |
| 3 | 64409600 | 64409600 | 0 | 64409600 至 64409600 | [199] | [1] |
| 4 | 64131072 | 64131072 | 0 | 64131072 至 64131072 | [199] | [1] |

每个扩展进程完成 357 次预期资源关闭回调、48 次合成模型调用。最终 scopes、instances、operations、pending operations、queued actions、leases、listeners、resources 均为零。该结果包含整个进程与分配器保留容量。

## 复现与证据

本次冻结源码提交为 `5d182941c827674dbf205b6307916eee89c858a5`，包含两处生命周期合同夹具修正。plan.json 在运行前固定样本、源码和 release 制品哈希，包含最终端点校验与环境凭据指纹修正。measurements/manifest.json 记录测量程序哈希、环境和源码哈希。source-current 是实际被测源码，baseline 使用 plan.json 指定的上一冻结目录。artifacts 保存实际被测库和原生程序。汇总核对 233 个源码文件，全部原始字节哈希与活动源码及冻结副本一致，逐项哈希见 plan.json；差异数量为 0。

使用 source-current 中的 kb_platform.rag_testing.harness_performance，传入 --baseline-source、--baseline-library、--library、--native-benchmark 和 --extended 可重跑。--output 必须使用新目录。本节相对路径以 `reports/rust-plugin-v1/acceptance-performance-final-20261030-04/` 为根。从项目根重评既有测量使用 `uv run --no-sync python reports/rust-plugin-v1/acceptance-performance-final-20261030-04/summarize_performance.py --output <新目录>`；这只重建汇总，不执行新测量。逐文件复核使用 `uv run --no-sync python reports/rust-plugin-v1/acceptance-performance-final-20261030-04/verify_current_source.py`，验证 233 个活动源码、233 个冻结副本和三个制品的原始字节哈希及源码清单；任何差异均以非零退出码报告。已保存的 `source-verification.json` 为 PASS，差异列表为空。原测量与源码、制品副本保存在上述本地报告目录，Git 中的本文件提供审阅摘要。本轮没有新增模型、Embedding 或 judge 远端调用。

本轮实际被测 DLL 来自 `reports/rust-plugin-v1/final-check-py312-02/native/agent_kernel.dll`，SHA-256 为 `d8904344aeaf7b1f16c380afb6e3cec32c9f3224de348b6ef5a8df6329c02d7d`。原生 benchmark 可执行文件 SHA-256 为 `70167676cb15cc4acc62f6864faa83bd39c206159be0606f18a5e6d080006777`。预算仍采用 [R0 冻结记录](rust-plugin-provenance.md) 中的九项绝对值。
