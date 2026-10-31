# Rust 插件审查修复性能验收

九项既有工作量均通过冻结绝对预算。五轮 baseline/current 交替测量中，各项当前进程 p95 范围均低于 baseline，范围未重叠。新原生核心和完整离线业务没有旧版同等工作量对照，单独报告。

测量使用同机、同一 Python、release 制品与冻结源码副本。每侧五个独立进程。hot call 每进程 1000 个样本，scope 和 single-tool 各 100 个，预热各 20 次。失败样本保留在分母，本轮错误为零。预检系统 CPU 忙碌率为 0.65%，逻辑 CPU 数为 24。

## 既有工作量与固定预算

单位为微秒。p95 是五个进程 p95 的中位数，范围为这五个值的最小值和最大值。验收按当前五个进程 p95 的最大值核对原有绝对预算，预算未改变。

| 工作量 | 规模 | 旧 p95 | 旧 p95 范围 | 当前 p95 | 当前 p95 范围 | 固定预算 |
| --- | ---: | ---: | --- | ---: | --- | ---: |
| hot_call_last_service | 1 | 555.0 | 540.4 至 573.4 | 88.5 | 77.7 至 92.1 | 953.4 |
| empty_scope_create_inherited_call_close | 1 | 1682.0 | 1641.2 至 1799.2 | 330.9 | 296.2 至 402.7 | 1997.6 |
| hot_call_last_service | 32 | 575.5 | 559.8 至 593.9 | 91.0 | 81.7 至 136.0 | 1073.3 |
| empty_scope_create_inherited_call_close | 32 | 1684.4 | 1615.1 至 1745.9 | 341.4 | 334.4 至 1098.0 | 2348.3 |
| hot_call_last_service | 128 | 553.9 | 518.3 至 577.6 | 104.8 | 95.7 至 114.9 | 758.2 |
| empty_scope_create_inherited_call_close | 128 | 1730.3 | 1542.3 至 1804.1 | 477.5 | 406.8 至 545.2 | 2091.8 |
| hot_call_last_service | 256 | 542.3 | 522.1 至 594.7 | 128.7 | 113.8 至 151.0 | 614.2 |
| empty_scope_create_inherited_call_close | 256 | 1720.5 | 1597.3 至 1878.8 | 588.4 | 549.1 至 620.4 | 1989.8 |
| single_tool_turn | 4 | 3935.1 | 3716.6 至 4478.1 | 1554.8 | 1523.9 至 1903.0 | 4469.3 |

## 新增工作量

以下结果只有当前实现。bridge 并发工作量以整批为时延单位，整批分别包含 1、4、16 次调用。完整业务包含冷组合、会话创建、真实 SQLite 提交、默认知识库图和清理。每轮业务执行四次合成模型调用并验证最终可见原文与持久 revision，远端调用为零。

| 工作量 | 并发 | p95 中位数 | p95 范围 |
| --- | ---: | ---: | --- |
| new_bridge_idle_stop_and_collect | 1 | 413.8 | 374.3 至 438.7 |
| new_bridge_cold_create_call_dispose_collect | 1 | 675.0 | 634.3 至 685.1 |
| new_bridge_dependency_replace_and_invoke | 1 | 547.8 | 528.6 至 631.1 |
| new_bridge_event_emit | 1 | 179.7 | 174.2 至 195.1 |
| new_bridge_event_parallel | 1 | 1124.5 | 1005.0 至 1176.1 |
| new_bridge_concurrent_calls | 1 | 214.3 | 194.9 至 254.9 |
| new_bridge_concurrent_calls | 4 | 823.9 | 770.5 至 914.3 |
| new_bridge_concurrent_calls | 16 | 3138.1 | 2899.9 至 3214.0 |
| new_offline_business_default_graph_with_durable_commit | 1 | 846832.8 | 750790.6 至 1129009.2 |

原生核心由五个新进程各执行 32 项组合，包含目录规模 1、32、128、256，冷生命周期、调用、scope、依赖替换、监听器和并发。所有回调计数符合预期，所有终结资源计数为零。完整结果见 analysis.json 的 native 列表和 measurements/native-process-*.json。

## 限制因素与内存

独立 profiler 在测量后执行，没有混入时延样本。其结果是定位成本的诊断，不能作为配对时延结论。

current profiler 总时间 1.696 秒，相关操作：native_call 33064 次，内部 0.482 秒，累计 0.939 秒；<built-in method nt._getfinalpathname> 246 次，内部 0.021 秒，累计 0.021 秒。

business profiler 总时间 3.795 秒，相关操作：native_call 2285 次，内部 0.039 秒，累计 0.074 秒；_getfinalpathname_nonstrict 10 次，内部 0.000 秒，累计 0.002 秒；<built-in method nt._getfinalpathname> 70 次，内部 0.005 秒，累计 0.005 秒；<method 'close' of 'sqlite3.Connection' objects> 87 次，内部 0.308 秒，累计 0.308 秒；<method 'commit' of 'sqlite3.Connection' objects> 87 次，内部 0.672 秒，累计 0.672 秒。

桥接成本主要在原生边界和 JSON 编解码。动态库路径已按配置缓存；Windows 路径解析只在初次加载等设置操作中发生。完整业务包含多次 SQLite 持久提交，文件系统等待影响其变化范围。微测量下降比例不能套用到完整业务或真实模型服务。

每个新进程预热 20 次后重复创建、调用、关闭并收集 200 个根运行时，每 20 次完成后读取 Windows 当前 PrivateUsage、WorkingSetSize、句柄数和 Python 线程数。以下为进程观测，不以累计峰值推算单个 Rust 内核内存。

| 进程 | PrivateUsage 首值 | 末值 | 增量 | 范围 | 句柄数 | Python 线程数 |
| ---: | ---: | ---: | ---: | --- | --- | --- |
| 0 | 66035712 | 66035712 | 0 | 66035712 至 66035712 | [199] | [1] |
| 1 | 64253952 | 64253952 | 0 | 64253952 至 64253952 | [199] | [1] |
| 2 | 64286720 | 64286720 | 0 | 64286720 至 64286720 | [199] | [1] |
| 3 | 64339968 | 64339968 | 0 | 64339968 至 64339968 | [199] | [1] |
| 4 | 64368640 | 64368640 | 0 | 64368640 至 64368640 | [199] | [1] |

每个扩展进程完成 357 次预期资源关闭回调、48 次合成模型调用。最终 scopes、instances、operations、pending operations、queued actions、leases、listeners、resources 均为零。该结果包含整个进程与分配器保留容量。

## 复现与证据

plan.json 在运行前固定样本、源码和 release 制品哈希，包含最终端点校验与环境凭据指纹修正。measurements/manifest.json 记录测量程序哈希、环境和源码哈希。source-current 是实际被测源码，baseline 使用 plan.json 指定的上一冻结目录。artifacts 保存实际被测库和原生程序。汇总核对 236 个源码文件，全部原始字节哈希与活动源码及冻结副本一致，逐项哈希见 plan.json；差异数量为 0。

本节相对路径以 `reports/rust-plugin-review-v1/performance-final-01/` 为根，冻结生产源码提交为 `6fd9ab8a57ed19c420220327769beec1712f7e62`。使用已提交的 `kb_platform.rag_testing.harness_performance`，传入 `--baseline-source`、`--baseline-library`、`--library`、`--native-benchmark` 和 `--extended` 可重跑，`--output` 必须使用新目录。具体冻结计划和启动命令见 `plan.json` 与 `launch.ps1`。

从项目根重评已有测量：`uv run --no-sync python reports/rust-plugin-review-v1/performance-final-01/summarize_performance.py --output <新目录>`。它只重新汇总，不执行测量。逐字节复核命令为 `uv run --no-sync python reports/rust-plugin-review-v1/performance-final-01/verify_current_source.py`。236 个当前源码、197 个原始基线文件、基线归档和三个制品的原始字节及清单均匹配，差异为空，实际记录见 `source-verification-after-analysis.json`。本轮没有新增模型、Embedding 或 judge 远端调用；合成模型调用另计。Git 中保存本摘要，冻结副本、原始测量和制品位于上述本地目录。
