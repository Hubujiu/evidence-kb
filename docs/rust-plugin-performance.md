# Rust 插件迁移最终性能验收

九项既有工作量均通过冻结绝对预算。五轮 baseline/current 交替测量中，各项当前进程 p95 范围均低于 baseline，范围未重叠。新原生核心和完整离线业务没有旧版同等工作量对照，单独报告。

测量使用同机、同一 Python、release 制品与冻结源码副本。每侧五个独立进程。hot call 每进程 1000 个样本，scope 和 single-tool 各 100 个，预热各 20 次。失败样本保留在分母，本轮错误为零。预检系统 CPU 忙碌率为 0.33%，逻辑 CPU 数为 24。

## 既有工作量与固定预算

单位为微秒。p95 是五个进程 p95 的中位数，范围为这五个值的最小值和最大值。验收按当前五个进程 p95 的最大值核对原有绝对预算，预算未改变。

| 工作量 | 规模 | 旧 p95 | 旧 p95 范围 | 当前 p95 | 当前 p95 范围 | 固定预算 |
| --- | ---: | ---: | --- | ---: | --- | ---: |
| hot_call_last_service | 1 | 545.0 | 534.4 至 582.2 | 69.3 | 60.3 至 70.4 | 953.4 |
| empty_scope_create_inherited_call_close | 1 | 1542.6 | 1508.9 至 1744.2 | 260.8 | 237.3 至 289.4 | 1997.6 |
| hot_call_last_service | 32 | 569.8 | 533.2 至 622.7 | 70.2 | 61.7 至 73.6 | 1073.3 |
| empty_scope_create_inherited_call_close | 32 | 1772.4 | 1682.2 至 2961.3 | 304.9 | 256.8 至 337.2 | 2348.3 |
| hot_call_last_service | 128 | 543.1 | 530.2 至 664.5 | 88.2 | 75.8 至 92.6 | 758.2 |
| empty_scope_create_inherited_call_close | 128 | 1726.6 | 1566.9 至 2012.3 | 396.7 | 368.8 至 429.8 | 2091.8 |
| hot_call_last_service | 256 | 540.5 | 526.9 至 691.0 | 106.4 | 96.4 至 116.0 | 614.2 |
| empty_scope_create_inherited_call_close | 256 | 1741.3 | 1586.1 至 2021.9 | 522.1 | 515.8 至 597.0 | 1989.8 |
| single_tool_turn | 4 | 3891.0 | 3595.9 至 6986.5 | 1455.1 | 1378.5 至 1514.0 | 4469.3 |

## 新增工作量

以下结果只有当前实现。bridge 并发工作量以整批为时延单位，整批分别包含 1、4、16 次调用。完整业务包含冷组合、会话创建、真实 SQLite 提交、默认知识库图和清理。每轮业务执行四次合成模型调用并验证最终可见原文与持久 revision，远端调用为零。

| 工作量 | 并发 | p95 中位数 | p95 范围 |
| --- | ---: | ---: | --- |
| new_bridge_idle_stop_and_collect | 1 | 374.8 | 359.8 至 411.8 |
| new_bridge_cold_create_call_dispose_collect | 1 | 618.6 | 581.9 至 700.3 |
| new_bridge_dependency_replace_and_invoke | 1 | 463.4 | 461.7 至 489.6 |
| new_bridge_event_emit | 1 | 123.3 | 114.6 至 135.0 |
| new_bridge_event_parallel | 1 | 967.6 | 853.1 至 1016.8 |
| new_bridge_concurrent_calls | 1 | 180.5 | 158.8 至 214.5 |
| new_bridge_concurrent_calls | 4 | 715.9 | 612.8 至 821.7 |
| new_bridge_concurrent_calls | 16 | 2808.4 | 2487.0 至 4232.0 |
| new_offline_business_default_graph_with_durable_commit | 1 | 812825.4 | 473514.8 至 1933579.3 |

原生核心另有 32 项组合，每项五个新进程，包含目录规模 1、32、128、256，冷生命周期、调用、scope、依赖替换、监听器和并发。所有回调计数符合预期，所有终结资源计数为零。完整结果见 analysis.json 的 native 列表和 measurements/native-process-*.json。

## 限制因素与内存

独立 profiler 在测量后执行，没有混入时延样本。其结果是定位成本的诊断，不能作为配对时延结论。

current profiler 总时间 1.466 秒，相关操作：native_call 28384 次，内部 0.415 秒，累计 0.797 秒；<built-in method nt._getfinalpathname> 246 次，内部 0.020 秒，累计 0.020 秒。

business profiler 总时间 3.664 秒，相关操作：native_call 2171 次，内部 0.038 秒，累计 0.071 秒；_getfinalpathname_nonstrict 10 次，内部 0.000 秒，累计 0.002 秒；<built-in method nt._getfinalpathname> 70 次，内部 0.005 秒，累计 0.005 秒；<method 'close' of 'sqlite3.Connection' objects> 87 次，内部 0.303 秒，累计 0.303 秒；<method 'commit' of 'sqlite3.Connection' objects> 87 次，内部 0.677 秒，累计 0.677 秒。

桥接成本主要在原生边界和 JSON 编解码。动态库路径已按配置缓存；Windows 路径解析只在初次加载等设置操作中发生。完整业务包含多次 SQLite 持久提交，文件系统等待影响其变化范围。微测量下降比例不能套用到完整业务或真实模型服务。

每个新进程预热 20 次后重复创建、调用、关闭并收集 200 个根运行时，每 20 次完成后读取 Windows 当前 PrivateUsage、WorkingSetSize、句柄数和 Python 线程数。以下为进程观测，不以累计峰值推算单个 Rust 内核内存。

| 进程 | PrivateUsage 首值 | 末值 | 增量 | 范围 | 句柄数 | Python 线程数 |
| ---: | ---: | ---: | ---: | --- | --- | --- |
| 0 | 65798144 | 65798144 | 0 | 65798144 至 65798144 | [199] | [1] |
| 1 | 64118784 | 64118784 | 0 | 64118784 至 64118784 | [199] | [1] |
| 2 | 64008192 | 64008192 | 0 | 64008192 至 64008192 | [199] | [1] |
| 3 | 64135168 | 64135168 | 0 | 64135168 至 64135168 | [199] | [1] |
| 4 | 64204800 | 64204800 | 0 | 64204800 至 64204800 | [199] | [1] |

每个扩展进程完成 357 次预期资源关闭回调、48 次合成模型调用。最终 scopes、instances、operations、pending operations、queued actions、leases、listeners、resources 均为零。该结果包含整个进程与分配器保留容量。

## 复现与证据

plan.json 在运行前固定样本、源码和 release 制品哈希，包含最终端点校验与环境凭据指纹修正。measurements/manifest.json 记录测量程序哈希、环境和源码哈希。source-current 是实际被测源码，baseline 使用 plan.json 指定的上一冻结目录。artifacts 保存实际被测库和原生程序。汇总核对 233 个源码文件；1 个文件在测量后仅有格式变化，Python 语法树一致，逐项哈希见 analysis.json。

本节的相对路径以 `reports/rust-plugin-v1/acceptance-performance-final-20261030-03/` 为根。使用 source-current 中的 kb_platform.rag_testing.harness_performance，传入 --baseline-source、--baseline-library、--library、--native-benchmark 和 --extended 可重跑。--output 必须使用新目录。从项目根重评本次既有测量使用 `uv run --no-sync python reports/rust-plugin-v1/acceptance-performance-final-20261030-03/summarize_performance.py --output <新目录>`；这只重建汇总，不执行新测量。原测量与源码、制品副本保存在上述本地报告目录，Git 中的本文件提供审阅摘要。本轮没有新增模型、Embedding 或 judge 远端调用。
