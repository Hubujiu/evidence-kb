# Rust 插件行为来源与验收基线

本记录对应 `evidence-kb-rust-plugin-execution-plan.md` 的 R0。实施从本地提交 `cbd8623f4907ae36e0e1b65bb990fa3e33875620` 开始。2026-10-30 核对 GitHub 后，`main` 仍为 `39199c0b1592f02ce3b8c5ebdde2d3aa43c05f4e`，PR #14 仍为 OPEN，HEAD 为 `27f7001f103479d5327b1e6a7efc7809ee3746df`。本地包含后续修复，模型能力只移入 #14 的模型模块并在当前业务入口接线，不能用其旧版业务文件覆盖本地修复。

## 固定来源

主要语义来源是 [DSH ce7073d vendor](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/README.md)。该副本记录 Cordis 4.0.0-rc.11 的上游来源 `0b96d9fd517b998e44e2209560c9e5f0671ce933`，并含 V6、V15、V25 等本地修改。补充测试来源是 [Cordis bb3bf8a core tests](https://github.com/cordiverse/cordis/tree/bb3bf8ad7a47f61c728c42157fc74c34bb31c1f2/packages/core/tests)。二者不能当作同一个版本。

固定文件及 SHA-256 保存在 `reports/rust-plugin-v1/baseline-20261030-01/reference-sources/` 和 `r0-audit.json`。目录包含 vendor 说明、DSH 的 fiber、events、reflect 和生命周期与配置测试、MIT 许可证，以及三个 Cordis 测试文件。

本轮还独立执行了该 DSH 提交中真实的 Cordis 与 Cosmokit 核心源码，生成 [九个固定参考轨迹](../config/plugin-runtime/cordis-traces-v1.json)。[来源与依赖锁](../config/plugin-runtime/cordis-reference-lock.json) 固定 20 个原始源码、manifest 和许可证文件的字节哈希，以及 Node `v22.23.1` 和生成器的文本哈希。运行时依赖仅为同提交的 Cosmokit，`@standard-schema/spec` 是擦除的类型依赖。Node 的原生 TypeScript 转换及一个 bare-import 解析钩子执行未修改的源码，没有安装或启动整个 DSH 应用。DSH 应用声明的 Node 24.9 最低版本仍然是应用要求，本次核心场景运行不代表该应用已在 Node 22 上验收。

常规 checker 通过 `cordis_reference_contracts.py` 比较本轮 Rust/Python 桥的公开观测与这些锁定轨迹，不下载源码，也不依赖 Node。九个参考场景覆盖启动与依赖清理、缺失依赖出现和撤销、子作用域覆盖与父资源借用、emit 顺序与监听器移除、parallel 的必要先后约束、serial 和 bail 的严格终止条件、waterfall 中间件以及重复 next。并行场景比较偏序，不要求不同语言线程的无关完成日志同序。

| 行为 | 参考场景 | 本项目判定与差异 | 合同位置 |
| --- | --- | --- | --- |
| 初始化失败与恢复 | `fiber.spec.ts` 的失败后依赖刷新、显式 update 恢复 | 失败不因普通刷新自动无限重试。显式更新创建新代次。 | `rust/plugin-runtime/src/tests.rs` 的初始化与更新合同 |
| 替换期间迟到完成 | DSH `cordis-core-lifecycle.spec.ts` 的 superseded activation | 旧代次不发布服务，但其资源仍由原代次清理。 | `late_init_after_replacement_is_never_published_and_its_resources_are_cleaned` |
| 重复与冲突回报 | 项目桥协议新增 | 相同 OperationId 的相同回报幂等，冲突保留原终结结果。 | `completion_replay_is_idempotent_and_conflicts_do_not_rewrite_accepted_outcome` |
| 部分初始化与资源清理 | DSH V6 的 setup 与卸载竞争 | 资源创建后立即归 Rust 所有。只有已完成清理可从重试中去除。 | `partial_initialization_failure_owns_resources_and_retry_never_repeats_successful_cleanup` |
| 依赖撤销与传递清理 | `service-dispose.spec.ts` 的 disposing consumer 和 transitive dependencies | 消费者清理期间仍保留旧绑定，提供者最后释放。 | `pending_dependencies_activate_and_consumer_cleanup_pins_replaced_provider` |
| 清理错误 | `service-dispose.spec.ts` 的 cleanup error | 有意采用更严格合同。保留 CleanupFailed、剩余资源及仍需要的依赖，不沿用参考中错误后继续释放的成功语义。 | Rust 部分初始化清理合同，Python 模型清理重试合同 |
| 作用域与隔离 | `isolate.spec.ts` 的 own provisions、injected view、shared label | 服务域表达可见性和归属。子 scope 只关闭自有资源，不能当恶意插件沙箱。 | `scope_override_visibility_parent_ownership_and_listener_cleanup_are_explicit` |
| 配置候选与 apply | DSH V8、V15、V25 和 `cordis-config-access.spec.ts` | 校验失败零变更。执行外部动作后显示失败和目标状态，不承诺自动完整回滚。 | `invalid_apply_and_unknown_protocol_fields_leave_live_registration_unchanged` |
| 同步调用取消 | 项目桥协议新增 | 取消和 deadline 只通知。阻塞 Python 调用真正结束前仍持有租约和资源。 | `cancelled_expired_invocation_pins_resources_until_real_completion` |
| Tokio 原生异步停止 | 项目原生适配新增 | 通知取消后等待任务实际结束，不能把通知当作清理成功。 | `native_async_task_reports_cleanup_only_after_cancellation_has_finished` |
| 事件和 waterfall | DSH `cordis-core-lifecycle.spec.ts` 的 repeated waterfall delegation 与固定 events 源码 | 分别规定 emit、parallel、serial、bail、waterfall。重复 next、并行错误和卸载后残留监听器分别验收。 | Python 插件运行时合同和 Rust 监听器归属合同 |
| 模型子路由 | 项目 V1 决策及 #14 共享模型能力 | 同 adapter 再注册仍产生新绑定。撤销一个路由不重启 models 核心或其他路由。 | `model_service_contracts.py` 的 `ModelRouteLifecycleContracts` |
| 物理请求 | 现有 budget、provider admission 和 session 合同 | Rust 生命周期不能创建第二个账本或重试权威。UNKNOWN 和未结预留保留。 | 原有合同及 `model_service_contracts.py` 的 HTTP/Embedding 预检合同 |

表中合同位置是验证映射。是否通过以本轮正式 checker 结果为准。已实际运行上述九个参考场景，不能据此声称所有 Cordis 功能或其完整上游测试已通过。

参考生成器可在独立目录重现。已有来源会逐文件校验，`--download` 只允许取得锁中指定提交的缺失文件。旧输出不会覆盖。

```powershell
uv run --no-sync python scripts/generate_cordis_reference.py --workdir .local/cordis-reference-new --output reports/rust-plugin-v1/reference-new/cordis-traces.json --download
```

与参考有意不同的行为单独记录在固定轨迹中。清理错误保留 `CleanupFailed` 和仍需要的依赖。emit 观察者错误被记录并继续通知其他观察者。Python 的同步效果不能被取消通知硬中断。作用域使用显式句柄及注册身份，不复制 JavaScript Proxy，也不提供恶意插件隔离。serial、bail、waterfall 则沿用参考的终止和 next 合同，`False` 不终止，而数值 `0` 终止。

## 改动前检查与冻结输入

正式入口的首次执行因 shell 的 PATH 不包含 Cargo 而失败，保留于 `checks/`。固定工具链已安装在 `C:\Users\Hubujiu\.cargo\bin`。补齐本次进程 PATH 后，同一入口在 `checks-path-fixed/` 完整通过。Rust 格式、Clippy、release 构建与 Rust 测试通过，374 个 Python 合同零失败、零跳过，24 个评分器控制样本通过。动态库来自这次成功构建，并复制到该输出的 `native/`。旧库未参与替代。

以下命令重现检查。`--output` 必须指向不存在的新目录。

```powershell
$env:Path = 'C:\Users\Hubujiu\.cargo\bin;' + $env:Path
uv run --no-sync python scripts/check_project.py --output reports/rust-plugin-v1/new-baseline/checks
```

R0 盘点确认 `rag-history-v1/final-01` 有 240 条既有结果、240 个请求绑定和 243 个可用的历史完成回答。原文、问题、unit、逐题结果和裁判结果的五个文本哈希全部与历史审计匹配。该历史适配器按解码文本哈希记录输入，Windows CRLF 的原始字节哈希不同，不能据此错误认定内容漂移。控制 Suite、Plan 和 oracle trace 的原始字节哈希另行冻结在 `r0-audit.json`。

只读 SQLite 盘点发现 412 个统一账本，其中六个账本各保留一个 UNKNOWN，未发现 STARTED。盘点包含历史副本与前序账本，不能相加当作累计实际费用。没有修改这些账本、重发请求或清零预留。开始任务时另有两个已运行的 Python 进程，未从其进程名断言提供者已空闲。本轮只执行离线合同及合成测量，新增模型、Embedding、judge 调用均为零。

## 同机 release 性能基线与预算

`performance-release/manifest.json` 保存源码、动态库和测量程序哈希、Python、Rust、平台以及样本计划。`source-baseline.zip` 来自改动前提交，另一次 profiler 使用这份归档代码与冻结动态库。profiler 数据不混入下表时延。

每种工作量运行五个独立进程，每进程先预热 20 次。hot call 每进程 1,000 个样本，scope 和 single-tool turn 各 100 个样本。失败样本仍留在分母，本轮没有失败。单位为微秒。p50、p95 列均为各进程分位数的中位数，范围为五次进程 p95 的最小值和最大值。

| 工作量 | 目录规模 | p50 | p95 | p95 范围 | 固定绝对 p95 预算 |
| --- | ---: | ---: | ---: | ---: | ---: |
| hot call | 1 | 484.0 | 535.1 | 527.6 到 740.5 | 953.4 |
| hot call | 32 | 488.3 | 581.7 | 535.1 到 804.2 | 1,073.3 |
| hot call | 128 | 482.2 | 578.1 | 543.0 到 650.6 | 758.2 |
| hot call | 256 | 474.9 | 542.2 | 537.6 到 560.0 | 614.2 |
| scope 创建、父服务调用和关闭 | 1 | 1,429.8 | 1,641.6 | 1,480.6 到 1,739.1 | 1,997.6 |
| scope 创建、父服务调用和关闭 | 32 | 1,501.2 | 1,716.7 | 1,631.3 到 1,989.8 | 2,348.3 |
| scope 创建、父服务调用和关闭 | 128 | 1,507.0 | 1,692.9 | 1,616.6 到 1,854.2 | 2,091.8 |
| scope 创建、父服务调用和关闭 | 256 | 1,554.0 | 1,741.0 | 1,663.9 到 1,815.7 | 1,989.8 |
| single-tool turn | 4 | 3,677.2 | 3,976.8 | 3,696.7 到 4,071.6 | 4,469.3 |

预算在实现性能优化前固定，计算式为 `max(进程 p95) + max(p95 极差, p95 中位数的 10%)`。它用于同机、同工作量、同 release 配置的迁移验收，不进入依赖共享机器时延的正确性 CI。增加真实工作量后不能沿用这些预算，换机器也须保留旧预算并新增校准记录。比较差异小于进程间波动时报告无可分辨差异。

独立 profiler 的 4.637 秒样本中，Windows `_getfinalpathname` 占 1.905 秒，`nt.stat` 占 0.728 秒，`plugin_native.native_call` 累计 3.955 秒。热点映射到每次 ABI 调用重复解析动态库路径。因此本基线主要观测 Python 桥与 Windows 文件系统调用成本，不能当 Rust coordinator 独立延迟。每次调用确实执行了原生协议并检查返回值。

原测量工具在 Windows 不提供 RSS，所有内存列为 null。本记录没有以累计峰值 RSS 推导单个运行时内存，也没有宣称真实请求或 RAG 延迟收益。

重现既有测量使用以下命令。新实现使用本轮新构建库，不能把旧库填入新结果。

```powershell
uv run --no-sync python -m kb_platform.rag_testing.harness_performance --output reports/rust-plugin-v1/new-performance --library <本轮原生库绝对路径> --build-profile release --processes 5 --samples 1000 --scope-samples 100 --loop-samples 100 --warmup 20
```

原生核心独立测量、完整离线业务时延、Windows 稳态 private bytes、真实 HTTP 中间件、受控 PostgreSQL、解析子进程和 Ubuntu 与 Python 3.13 矩阵在这份改动前记录中尚未执行。后续实现必须以独立结果补齐，不能由本表或 default-path 服务回放代替。
