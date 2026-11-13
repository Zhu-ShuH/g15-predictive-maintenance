# 测试计划：独立验证与报告（Issue #9）

> 负责人：B1（kittywent，2026-10-25 承接原 C5 职责）
> 依据：`docs/acceptance.md` §3/§4、`docs/interface.md` §5.3/§5.4/§8.5、`docs/development.md`
> 本文件是**计划**，实测结果见 `docs/test_report.md`。计划里写的任何数字都不是成绩。

## 1 验证目标与独立性

教师硬指标要求给出"受控故障检出率"，并且验证必须独立。本计划的独立性由三条约束保证：

1. 只有 `g15.reporting` 读 `private_truth/faults.json`；`simulation`、`pipeline`、`analysis`、`storage` 全程不读真值（见 §6.4 的可证伪用例）。
2. 匹配由报告层（`reporting.builder` / `reporting.metrics`）按 `device_id` 与事件区间自行计算，被测代码不产出 `event_id`，因此"检出"不是被测方自报的；`reporting.verifier` 与 `g15 verify` 只做完整性校验，不读真值。
3. seed `42` 与 `20261016` 均为开发期已暴露数据（B2 已披露查看过其特征分布），只作开发/回归证据，不构成独立保留验证；`configs/health_rules.yaml` 在验证前已提交并记录摘要（§2），本轮未修改任何规则、阈值或测试期望。

## 2 被测身份与配置摘要

| 项 | 值 |
|---|---|
| 基线提交 | `75b119454a1ac075620bdcc74826730fa7b4ac02` |
| `configs/health_rules.yaml` SHA-256（工作树 CRLF 字节） | `c26bad78809424599a48ccebd7f92b1b290e9d598a0367778127c9e1b9c77f1f` |
| `configs/health_rules.yaml` SHA-256（Git LF blob 字节） | `2b3a2763c72e32abd276b2d2617224b8bb941ca5d82807d2258db8d4c3f92d8b` |
| `configs/consumer.yaml` SHA-256（工作树 CRLF 字节） | `aebb18a9191f154c16e1320a73d7593774879d44573d5c10811fea8e35c08bac` |
| `configs/consumer.yaml` SHA-256（Git LF blob 字节） | `8006460052a9b741c1595ec94e5c487e99e54931315bc87482c9b197a5adde8c` |
| `configs/benchmark_1m.yaml` SHA-256（工作树 CRLF 字节） | `47ceeee975bae5ef0a47dddcb00b4468fb74430fcd6e5cee5a906640e0b4c1ab` |
| `configs/benchmark_1m.yaml` SHA-256（Git LF blob 字节） | `fa7ad03a153446707fd4aa49c6bccb5915a578eff471d6477d577c36c44ebd05` |
| `scoring_version` | `heuristic-v1` |
| `rule_id` | `health_heuristic` |
| `window_points` W | 256（**不写死**：报告一律从已存窗口几何读出，见 §6.5） |
| `max_detection_lag_windows` | 2 |

> **字节口径**：上表两列分别是「本机工作树按 CRLF 检出后的字节」与「Git 仓库内 LF blob 的字节」。两者不同只说明换行符差异，不是配置内容改动；A 已核收并作为后续整改核对基线（Issue #56 评论 `<!-- A-T83-config-receipt -->`）。

被测模块：`g15.pipeline.producer` → `g15.pipeline.consumer` → SQLite → `g15.reporting.builder` / `g15.reporting.metrics`。
不测：双 Linux 部署与运维留证（属 Issue #10 / C6）；实时网络接口（本系统不要求）。

> **证据性质**：本计划的 seed=42 / seed=20261016 均为开发期已暴露数据，按 Issue #56 第 1 条只作开发/回归证据，不称独立保留集。未替换 seed、未新增轮廓、未改阈值。执行顺序要求（#56 第 2/5 条）：先固定配置与规则摘要并经 A 审阅，再跑；本轮配置摘要已由 A 核收（`<!-- A-T83-config-receipt -->`），但 A 明确不追认未见证的先后顺序，故本轮结果**不主张**"跑前已完成审核固定"。

## 3 用例矩阵

固定轮廓来自 `docs/acceptance.md` §3：单设备、100 Hz、`window_points=256`、`chunk_points=10240`、`total_points=65536`、`incomplete_window=drop`；两个信号基线/噪声为 0.5/0.05 g、35/0.15 °C。

| 用例 | 配置 | 注入 | 通过条件 |
|---|---|---|---|
| 正常 ×2 | `acceptance_healthy_seed42.yaml`、`acceptance_healthy_seed20261016.yaml` | 无事件 | 告警数 0；`detection_rate` 为 `null`（不得填 100%） |
| 缓慢退化 ×2 | `acceptance_degradation_seed42.yaml`、`acceptance_degradation_seed20261016.yaml` | 退化率 0.00002 g/采样 | 后半段健康分中位数低于前半段；退化场景的告警不计入正常场景误报门槛 |
| spike ×2 | `acceptance_spike_seed42.yaml`、`acceptance_spike_seed20261016.yaml` | 单事件 `start_seq=16384`、`duration_points=256`、`amplitude=5.0` | 该事件被检出 |
| step ×2 | `acceptance_step_seed42.yaml`、`acceptance_step_seed20261016.yaml` | 单事件 `start_seq=16384`、`duration_points=2048`、`amplitude=5.0` | 该事件被检出 |
| burst ×2 | `acceptance_burst_seed42.yaml`、`acceptance_burst_seed20261016.yaml` | 单事件 `start_seq=16384`、`duration_points=2048`、`amplitude=5.0` | 该事件被检出 |

三类波形 × 两个 seed = **6 个应检事件**，要求 TP=6、FN=0。N=1 连续触发窗只覆盖"能触发"，去抖/升级/恢复属 N>1 边界，本计划不冒称已覆盖。

### 3.1 三场景闭环报告的取证口径

`docs/acceptance.md` §4 要求「正常、退化、突发三场景生成 JSON + Markdown 闭环报告」，`docs/interface.md` §14 的 R08 同样只要「闭环联调报告」。这两条要的是**报告产出**，不是让演示配置去判「通过」。所以：

- 三场景用仓库自带的演示配置 `healthy.yaml`（`healthy_001`）、`degradation.yaml`（`degradation_001`）、`sudden_fault.yaml`（`sudden_fault_001`）跑通「特征 → 健康分 → 告警 → SQLite → 报告」全链，取证物是每个 run 目录下的 `reports/summary.json` 与 `reports/summary.md`。
- 这三个 id 不是 `docs/acceptance.md` §3 的受审验收配置，**不写进** `ACCEPTANCE_SCENARIO_KINDS`。它们的 `scenario_kind` 为 `null`、`verifiable=false`、`passed=null` 是预期结果，不是缺陷；「通过/不通过」只由 §3 那 10 个受审配置给出。
- `benchmark_1m.yaml` 同理落 `passed=null`，它的验收口径是 §2 的百万点计时，不是事件检出。
- 有注入事件的演示配置（`bearing_001`、`sudden_fault_001`）仍按事件证据判 `fault` 并给出结论，不因为 id 不认识就退回 `null`。

把演示 id 塞进受审清单会让「报告产出了」被误读成「演示配置通过了验收」，所以明确拒绝这种做法。

### 3.2 场景类型的来源与 manifest 版本窗口（Issue #83）

报告里的 `scenario_kind` 决定套用哪一套验收判据，所以它只能来自 run 自己带得出的证据。三个来源按优先级取第一个可用的，并在 `scenario_kind_source` 里点名：

| 优先级 | 来源 | `scenario_kind_source` | 何时生效 |
|---|---|---|---|
| 1 | `private_truth/faults.json` 有注入事件 | `fault_events` | 事件本身就是证据，即使 manifest 也声明了 |
| 2 | `manifest.json` 的 `scenario_kind` | `manifest` | schema 2.0 生产端写下声明，且真值没有事件 |
| 3 | 受审验收清单 `ACCEPTANCE_SCENARIO_KINDS` | `acceptance_manifest` | 仅作 schema 1.0 历史 run 的迁移回退 |
| — | 三者都没有 | `null` | 判据不套用，`verifiable=false`、`passed=null` |

来源与真值冲突时不按上表静默取一个：两种情况都记 blocker，`verifiable=false`、`passed=null`，责任写进 `verdict_reasons`。① 声明 `normal`/`degradation` 而真值有事件；② 声明 `fault` 而真值零事件（后者若照 fault 判据打分会报出「检测率不达标」，但根本没有事件可漏，错的是声明不是检测器）。guard 见 `tests/integration/test_validation_report.py` 的两个矛盾档用例。

manifest 侧的版本窗口：生产端只写 `2.0`；读入端接受 `{1.0, 2.0}`，窗口外（如 `3.0`）在键集校验**之前**就点名拒收。`scenario_kind` 是可选键，缺键 = 未声明（不是 `normal`），显式 `null` 与非法取值都会被读入侧拒收。1.0 的 run 仍可长期读入，`scenario_kind` 缺失时走第 3 条回退。

配置侧只允许 `scenario.kind` 取 `normal` / `degradation`；写 `fault` 直接 `ConfigError`，因为 fault 只能由真实注入事件得出。声明 `normal`/`degradation` 又同时要求注入事件的配置自相矛盾，生产端在创建 run 目录之前就拒绝。

## 4 匹配细则（`docs/acceptance.md` §3 的可执行化）

1. 真值事件是半开区间 `[s, e)`，仅报告层（`reporting.builder` / `reporting.metrics`）读取用于真值对账；`reporting.verifier` 与 `g15 verify` 只做完整性校验，不读真值。
2. 允许告警窗口起点区间为 `[floor(s/W)×W, ceil(e/W)×W + 2W)`，W=256，`max_detection_lag_windows=2`；用告警的 `window_start_seq`，不用机器当前时间。
3. 必须同 `device_id`；告警按 `(device_id, rule_id, start_seq)` 去重；事件与告警**一对一**匹配，先求**最大匹配数**，多解按事件起点、`event_id`、告警起点、`rule_id` 排序选择。
4. `detection_rate = TP / 事件总数`；零事件返回 `null`。逐事件输出匹配、漏报、首次告警窗口、数据时延与宽限。
5. `false_alarm_count` 为不落入任一事件允许窗口的告警数；故障窗口内重复告警计入 `redundant_alarm_count`；`unmatched_alarm_count = false_alarm_count + redundant_alarm_count`，三个数分开报告。

这五条在 `tests/integration/test_metrics_matching.py` 里各有独立用例钉住（含"贪心会少检一个"的最大匹配反例），避免规则被实现悄悄放宽。

## 5 测试层级与执行命令

| 层级 | 位置 | 命令 |
|---|---|---|
| 单元 | `tests/unit/` | `python -m pytest -q tests/unit` |
| 集成（含匹配规则） | `tests/integration/` | `python -m pytest -q tests/integration` |
| 故障注入 | `tests/fault_injection/` | `python -m pytest -q tests/fault_injection` |
| 性能（opt-in） | `tests/performance/` | `python -m pytest -q tests/performance` |
| Issue 验收 | 集成 + 故障注入 | `python -m pytest -q tests/integration tests/fault_injection` |

集成与故障注入用例都跑真实链路（`load_config` → `produce_run_detailed` → `consume_run` → `build_report`），不手工拼 NPZ、不 fake 任何模块，因为报告的价值就在于描述管道**实际**存了什么。

### 5.1 生产结束后独立校验（`verify_run`，Issue #51 承接项）

`docs/acceptance.md` §2 要求生产结束后单独核验一次产物：点数、dtype、连续 `seq`、SHA-256 与 `complete` 清单。入口是 `g15.reporting.verifier.verify_run(run_dir, *, expected_total_points=None) -> VerificationResult`，与消费链路完全解耦。

| 项 | 口径 |
|---|---|
| 输入 | 只读 `run_dir`：`manifest.json` 与其列出的分片 |
| 不读 / 不写 | 不打开 SQLite、不读 `private_truth/`、不写任何文件 |
| 七项 checks | `manifest.complete`、`parts.contiguous`、`rows.sum`、`shard.dtype`、`shard.seq`、`shard.hash`、`seq.cross_shard` |
| 失败分层 | 清单结构性损坏 → `VerificationError`（无法验证）；清单可读但分片内容损坏 → `passed=False`，`failures` 逐条给码，一次报全不早停 |
| 耗时 | `VerificationResult.elapsed_seconds` 在本次调用内计时，与生产计时窗口不重叠 |

**明确不覆盖**：`timestamp_ns` 的绝对时间公式（`start + seq / sample_rate`）不在七项 checks 内——整列置 0 或倒序并重算摘要后仍可能 `passed=True`。原始完整性通过不等于时间轴核验，也不等于检出率、误报或性能。

用例在 `tests/integration/test_run_verifier.py`（41 个测试函数，`--collect-only` 收集 58 项；其中符号链接用例在无权限平台 skip）。端到端用例 `test_the_acceptance_run_verifies_end_to_end` 走 `load_config` → `produce_run_detailed` → `verify_run`，不手工拼产物。

### 5.2 压力测试（重复执行、并发与中断恢复，国标「压力测试」项）

国标要求测试覆盖单元、集成、性能、压力四类。前两类见 §5，性能见 `tests/performance/`（opt-in），本节补**压力**。本项目的压力面不是并发连接数，而是**同一份产物被反复生产、反复消费，以及在多进程争用与中途被杀之后还能不能收敛到同一个结果**。

用例在 `tests/integration/test_stress_repeatability.py`（4 个测试函数），与性能测试一样 opt-in：

```bash
G15_RUN_STRESS=1 python -m pytest -q -s tests/integration/test_stress_repeatability.py
```

不设环境变量时 4 条全部 skip，默认门禁不因压力轮变慢。

| 用例 | 做法 | 判据 |
|---|---|---|
| `test_repeated_production_lands_byte_identical_shards` | 同一配置连跑 3 次，每次独立 run 目录 | 3 次的 `(part_id, rows, sha256)` 逐项相同；分片内容确定，后面的重放比较才有意义 |
| `test_repeated_consumption_adds_no_row` | 同一 run 首轮消费后再 `consume_run` 2 次到同一个库 | 后两轮 `windows_written=0`、`alarms_written=0`、`shards_processed=0`，`shards_skipped` 等于首轮分片数，五张表行数不变 |
| `test_racing_consumers_never_duplicate_rows` | 3 个 spawn 子进程同时消费同一 run 同一库 | 不论谁先提交，库内容必须等于单进程跑一遍的结果；成功者写入的窗口总数等于单遍总数，不出现重复分片或半写窗口 |
| `test_abruptly_stopped_consumption_replays_to_the_same_content` | 子进程消费 `benchmark_1m`（10 分片），首个分片事务提交后 kill，再正常重放 | 停在第 1 片之后、最后一片之前（`1 <= processed_chunks < 10`）；重放后 `runs`/`processed_chunks`/`feature_windows`/`alarms`/`alarm_states` 五表全量行与未中断的对照库逐行相同 |

并发用例只看**不变量**：`consume_run` 没把并发消费写成受支持的能力，所以用例不要求 3 个进程都成功，只要求失败者不留半写状态、成功者合起来正好写满一遍。实测里失败方抛 `IntegrityError`（唯一键冲突）或 `DatabaseError`（SQLite 锁），是拒绝而不是脏写。

被 kill 的子进程会留下未回滚的 WAL，紧接着 `connect` 可能报 `disk I/O error`。这是 SQLite 的恢复窗口，不是消费者缺陷，所以用例读取前用 `_retry_while_recovering` 重试到 60 s 超时为止，超时才判失败。

**不覆盖**：

- **Linux `kill -9`**：Windows 上 `Process.kill()` 是 `TerminateProcess`，没有 POSIX 信号语义。本节的中断恢复只是同属性的补充证据，`docs/acceptance.md` §4 要求的 `kill -9` 仍归 `tests/fault_injection/test_pipeline_faults.py` 并在 Linux 上跑（Issue #10）。
- **长时间浸泡（soak）**：没有小时级连续运行用例。
- **峰值 RSS 与 CLI 总墙钟**：分别归 C6 的进程外工具与 C3 的 `benchmark` 子命令，本节不测。
- **更高并发度**：只固定 3 个消费者，没有扫并发梯度。

### 5.3 生产基准的百万点计时（Issue #51 承接项）

`docs/acceptance.md` §2 的百万点门槛（<60 s）属生产基准口径，`benchmark_1m.yaml` 不是事件检出用例，因此不参与 §3 的 `passed` 判定。本节只登记**怎么测**，数字在 `docs/test_report.md` §5.3。

| 项 | 口径 |
|---|---|
| 被测入口 | `python -m g15 benchmark --config configs/benchmark_1m.yaml`（C3 的薄子命令，内部调 `produce_run_detailed` 与 `verify_run`，不在 CLI 重写公式） |
| 配置 | `1,000,000` 点、`seed=42`、`chunk_points=102400`、无注入事件 → 10 分片（`9x102400+78400`）、`2,000,000` 标量值 |
| 独立进程 | 每轮一个全新进程、一个独立空运行目录；不复用上一轮的 `runs/` |
| 三项数字分开 | ①生产窗口 `perf_counter`（完整生成、分片/真值写入、哈希、最后 `complete` 原子发布）②CLI 总墙钟（**进程外**计时，覆盖解释器启动）③生产结束后独立校验耗时 |
| 进程峰值 RSS | 由 C6 在 Linux 上用 `/usr/bin/time -v` 记录，是**整个被测进程的历史最高水位**，不是生产计时窗口专属峰值 |
| 不做 | 不用 `ru_maxrss` 前后相减当单次峰值（A 已否决：历史水位不复位）；不移动生产计时边界来缩短数字；Windows 结果只作预检，不代填 Linux 验收 |

子命令**故意不内置** `cli_wallclock_seconds`（`unmeasured`）：进程无法观察自己的解释器启动，该数字必须由进程外工具给出。

## 6 三类独立故障注入证据（`docs/acceptance.md` §4）

业务故障（三种波形）**不能**替代下面后两类可靠性测试。

### 6.1 业务故障
spike / step / burst 六个受控事件，见 §3。

### 6.2 文件损坏 / SHA 不匹配
在真实运行产物上做四种破坏，每次断言"拒收且未写入"：

| 破坏 | 期望 |
|---|---|
| 分片中间一个字节取反 | `ConsumptionError`，消息含 `sha256 is`；`feature_windows` 仍为 0 |
| 删除一个分片 | `ConsumptionError` 或 `OSError`；未写入 |
| 分片截断一半 | 拒收（`sha256` / `ConsumptionError` / `OSError` / `ValueError` 之一）；未写入 |
| 损坏后修复再跑 | 正常完成，窗口数等于首次成功写入数（证明库没被写坏） |

### 6.3 `kill -9` 恢复（Linux）
当前 a10cab2f 用例采用真实 COMMIT 后管道 checkpoint：
1. 测试子进程包装 `consumer.transaction`，执行原事务及真实 COMMIT；退出后确认不在事务中，独立只读连接核对恰有第一片 marker。
2. stdout 管道上报 `after_first_shard_commit` JSON（PID、源码位置、marker、`transaction_open=False`），然后阻塞读 stdin 管道。无 watcher、握手文件、固定 sleep 或生产 hook。
3. 父进程通过 `select` 等待并逐项核验，确认子进程存活、保存库快照后 SIGKILL，断言 `returncode == -SIGKILL`。
4. 击杀后断言 `partial == before_kill` 且恰第一片已提交。checkpoint 在两片事务之间，不证明未提交事务内击杀。
5. 普通未包装消费者重放，断言跳过 1 片、处理剩余 `total_parts - 1` 片，与未中断运行比较五张业务表全部可比内容。

Windows 仍 skip，不替代 POSIX Linux 实跑。C6 2026-11-02 在 main `7daa07c` 双 Linux 各 9 passed 对应旧 blob `19cbcab`，不是当前管道用例 `a250b5e`。本次文档修订未重跑 Linux 单文件，当前与历史证据分列，见报告 §4.3。

### 6.4 真值隔离（可证伪）
- 删除 `private_truth/faults.json` 后**消费仍然成功**（消费者不读真值），但报告必须判为**不可验证**（`verifiable=false`、`passed=null`、`detection_rate=null`），并给出"缺真值"的原因——不能把"文件没了"当成"没有事件"而判正常通过。
- 真值未知时，**一切依赖真值的计数一律为 `null`**：`fault_events`、`detected_events`、`missed_events`、`false_alarm_count`、`redundant_alarm_count`、`unmatched_alarm_count` 全部 `null`。特别地，不得把库里的告警算成"误报"——它本来可能命中那个看不见的事件。库侧的已知事实（`alarm_count`、`dropped_alarm_count`）单独保留，仍然报出。
- 只有当运行日志 `run_complete.fault_events` 明确写着 0 时，缺文件才被读作"零事件"。
- 真值文件被写坏（JSON 语法错或字段非法）时，**消费已成功**，`build_report` 抛 `ReportError`——证明真值只在报告层被读。
- 真值文件来自另一个 run（`run_id` 不符）时同样抛 `ReportError`，不允许张冠李戴。

## 7 通过标准

1. `python -m pytest -q tests/integration tests/fault_injection` 退出码 0。
2. 每个受控故障 run 的 `reports/summary.json` 与 `reports/summary.md` 同时产出，且两份数字一致。
3. 正常场景 `alarm_count=0`、`detection_rate=null`；故障场景 `detection_rate=1.0` 且 `false_alarm_count=0`。
4. 报告必须**同时**给出检出率与误报数，不得只报一个；未知指标写 `null`，不得捏造 0 或 100%。
5. **不可验就不得判通过**：缺真值（日志未声明 0 事件）、该 `run_id` 未消费、窗口宽度未知 → `verifiable=false`、`passed=null`、`detection_rate=null`，并在 `reasons` 写清原因。
6. **有 `runs` 行不等于消费完成**：`manifest.parts` 里每个分片都必须有 `processed_chunks` 行且 `sha256` 与清单一致；缺行或摘要不符即为**部分消费**，`verifiable=false`、`passed=null`，`pending_parts`/`digest_mismatch_parts` 列出具体片号。
7. **真值未知时不得声称零事件**：`fault_events` 等一切依赖真值的计数为 `null`；库侧 `alarm_count` 照常报出。
8. **窗口宽度一律从已存窗口几何读出**（`feature_windows` 的 `end_seq_exclusive - start_seq`），不写死 256；宽度不一致直接 `ReportError`。
9. **点数四项自洽**：`processed_points`（已提交片）+ `rejected_points`（未消费片）= `total_points`，`analysed_points`（真实入窗）+ `dropped_points`（片内未成窗）= `processed_points`。
10. **摘要冲突时口径必须统一**（R7）：`analysed_points`、`alarm_count` 与一切匹配指标只统计**有效片**（已提交且 `sha256` 与清单一致）的数据；`dropped_points` 因此不可能为负，**不得**用 `max(0, ...)` 掩盖。无法归属当前 manifest 的库中行不得静默丢弃，必须以 `raw_feature_windows` / `excluded_feature_windows` / `raw_alarm_count` / `excluded_alarm_count` 报出，把"库中原始记录"与"可归属当前 manifest 的数据"分开；`summary.json` 与 `summary.md` 必须同源。
11. 工程门禁：`black --check src tests`、`pylint src/g15 --fail-under=8.5`、`pytest -q` 全部退出 0。

## 8 明确不覆盖（不得在答辩中宣称）

- **双 Linux 实测（主链完成、最终发布待复证）**：`kill -9`（§6.3）、百万点基准（§5.3）与公开仓库全新克隆已有双系统留证；主链接通后的 `545a598c` 还在两机执行了 setup→clean→demo→verify→benchmark，均退出 0（#10 评论 `6759021839`）。仍未完成的是 #56/#9 独立保留集的双系统检出率/误报复现，以及在**最终候选 SHA**上的整包复证；本计划既有检出率数据不能冒充该结论。
- **百万点 60 秒的结论**：生产基准的计时方法见 §5.3，实测数字见 `docs/test_report.md` §5.3。本计划只负责受控轮廓（65536 点）的事件检出，两者口径不能互换。
- **真实工业准确率**：本计划的"100%"只指 §3 那套预定义教学测试集上的事件召回率，不代表现场设备表现。
- **去抖/升级/恢复（N>1）**：受控轮廓固定 N=1，不能用它冒充去抖已验证。
- **C5 之外的模块单测**：各模块单测由各自负责人交付，本计划只统计真实跑过的总数（§5），不代写。
## 9 独立保留集计划（未执行）

A 已裁决（Issue #56 评论 A-T151，2026-11-10）：采用「补充真正未参与开发的保留集」；方向 2（用开发/回归证据 + 方案 C + scenario_kind 收尾）不采纳。本节只登记计划与待评审建议，**尚未选择 seed、尚未生成数据、尚未运行任何新保留集**。

### 9.1 状态

- 未选择具体 seed 值；未生成、读取、运行任何新保留数据；旧集（42/20261016）全部结果与失败原样保留，不覆盖历史。
- `docs/adr/0006-independent-validation-baseline.json` 保持 `status=proposed_unfrozen`、`integrated_validation_commit=null`、`new_holdout_seed=null`、`new_holdout_generated=false`、`rules_frozen=false`。
- 本计划的「真值隔离 / 非自报匹配」证明的是隔离与匹配边界，不单独证明数据独立性。

### 9.2 两个新 seed 的矩阵（待评审建议，非已批准门槛）

- 两个尚未选择的新 seed，每个 seed 各含 normal、degradation、spike、step、burst；
- 合计 10 配置、6 故障事件（spike/step/burst 各一事件 × 2 seed）；
- 沿用口径：W=256、N=1、一对一最大匹配、允许区间右开且结束后 2 窗宽限；
- normal 为 0 告警；degradation 单独报告，不并入 normal 误报；
- FP / redundant / unmatched / FN、逐事件映射及全部失败原始证据均保留。

数量、矩阵与记录结构须经 B1 计划、B2/C1 反馈、C4 与非作者 Review、A 审核后才可能进入标准。

### 9.3 记录结构建议（两 seed 方案）

现有 baseline JSON 只有单数 `new_holdout_seed`。若保留两 seed 方案，建议改为可审查的数组结构（本 PR 只提建议，不改 JSON）：

- `new_holdout_seeds`：数组，每项含 `seed`、`selection_method`、`selected_at`、`entropy_source`、`contact_declarations`（B2/C1/执行方对具体值的本人披露）、`configs`（该 seed 下 5 份配置路径与 git blob / raw SHA-256）；
- `rules_frozen`、`integrated_validation_commit` 仅在 A 记录真实冻结后写入，不静默填值、不倒填历史附件。

### 9.4 seed 选择方法与接触声明（未执行）

- 选择方法经评审后，只产生候选**值**，仍不生成数据；
- 候选值产生后，B2、C1、执行方 B1 及任何生成者/保管者须对具体候选值作本人接触声明；任一候选有接触记录，即在首次数据生成前作废并另选；
- A 不代任何角色签署「未接触」。

### 9.5 Review 与冻结顺序（A-T151 七步）

1. B1 提交本计划 PR（未执行）；2. C4 复核真值隔离、匹配口径、seed 选择方法、负向/边界测试，并取得至少一位非作者 Review；3. A 只批准 seed 选择方法，不批准尚不存在的值或结果；4. 按已批准方法产生候选值（不生成数据），各角色对具体值作接触声明；5. 获确认候选值对应的精确配置/测试差异补入 PR，再次 Review；6. 全部进入同一集成提交后 A 记录真实冻结（含 `integrated_validation_commit`、配置 blob/SHA、规则摘要、环境、匹配参数、接触声明与 Review 链接）；7. A 记录 `rules_frozen=true` 后才首次生成并运行保留数据。

首次运行须保留命令、退出码、环境、固定 SHA、完整原始输出、逐事件映射与所有失败；看到结果后修改规则、阈值、配置、验证器语义或测试期望，该保留集即转已暴露开发数据，须重新 Review、重新冻结、换新 seed。
### 9.6 测试轮廓（待 seed 选定后实现，未生成数据）

- 位置：`tests/integration/test_holdout_validation.py`（本 PR 不建文件、不写测试代码，仅登记轮廓）；
- 复用 §3 匹配判据：TP=6 / FN=0、normal 零告警且 `detection_rate=null`、degradation 单独报告不并入 normal 误报、FP / redundant / unmatched 分开；
- 负向/边界（C4 复核点）：真值隔离（§6.4）、非自报匹配（§4）、窗口右开 + 2 窗宽限、一对一最大匹配；
- 数据仅在 §9.5 完成 A 冻结后，才由新 seed 经同一受控轮廓（单设备、100 Hz、`window_points=256`、`chunk_points=10240`、`total_points=65536`）首次生成并运行。选择方法获批、候选 seed 选定且本人接触声明齐备后，可以先编写未执行的精确配置与测试，须经再次 Review 并进入同一集成 SHA，再由 A 冻结；冻结前不得生成、读取或运行新保留数据。

## 10 期末 100 分标准的现场验收增量（Issue #142）

本节按 GB/T 15532-2008 所强调的可执行用例要素登记“编号、前置、步骤、期望、实际/证据”。这里是项目内追踪映射，不声称通过标准认证。实际栏未绑定最终候选 SHA 前一律写“待执行”，历史证据只能作为预检。

| ID | 前置条件 | 现场步骤 | 期望结果 | 实际/证据状态 |
|---|---|---|---|---|
| FT-NET-01 | 已在目标 Linux 完成依赖安装；记录 SHA/网络状态 | 禁用网络，执行 `clean.sh → demo.sh → verify.sh`，同时记录出站连接 | 主链退出 0；无运行期联网依赖；产物与联网运行口径一致 | **最终 SHA 待执行**；“设计为离线”不算实测 |
| FT-DATA-01 | 生成一个完整 run 并复制对照 | 翻转已发布 NPZ 一个字节，再运行 consume/verify | SHA/Schema 拒收、退出非零、无新增可信窗口；修复后可恢复 | 自动化与历史证据已有；最终 SHA 现场复跑 |
| FT-KILL-01 | Linux，10 分片 run，建立未中断对照库 | 首个分片已提交且未完成时发 `SIGKILL`，确认部分前缀，再重启 | 子进程确因 SIGKILL 退出；重放后五表逐行等于对照 | 双 Linux 历史证据已有；最终 SHA 现场复跑 |
| ACC-IDEMP-01 | 完成一次消费 | 对同一 run/库再消费两次 | 第二、三次不新增分片、窗口或告警 | 自动化已有；现场记录五表计数 |
| ACC-RECON-01 | 完整或受控部分消费 run | 生成报告并读取 JSON/Markdown | `processed+rejected=total`；`analysed+dropped=processed`；两份报告同源 | 自动化已有；最终 SHA 现场保存产物 |
| PERF-1M-01 | 空运行目录、固定 benchmark 配置、目标 Linux | 每系统独立进程连续运行 3 次 `benchmark.sh` | 每轮 1,000,000 点、10 分片、独立校验通过、生产窗口严格 <60s；报告中位/最大/RSS | 双 Linux 历史证据已有；最终 SHA 重跑 |
| PERF-QUERY-01 | #135 已实现（#149）；#136 Gate 0、D0 数据待执行 | 按 #136 Gate 0 跑冷热查询、oracle 与 `EXPLAIN QUERY PLAN` | 响应逐项等于独立 SQL；给出 p50/p95/p99、计划、资源、计数守恒 | **#135 已实现；#136 Gate 0 未执行**；禁止以 ADR 目标代替数据 |
| SEC-LOG-01 | 完成一轮主链及已实现查询服务 | 扫描仓库外运行日志/报告 | 无 token、密码、真实个人身份、真实厂区信息、私人绝对路径或 `private_truth` 原文 | 主链最终 SHA 待执行；查询服务已实现（#149） |
| SAFE-NOACT-01 | 使用发布 CLI/脚本清单 | 搜索并演示告警、reset 边界 | 系统无停机/控制命令；reset 必须 `--yes`+原因；任何未来执行动作需人工确认 | no-actuation 为架构边界；reset 自动化已有 |

### 10.1 失败、降级与补偿记录

每个现场用例必须同时保存失败与恢复证据，不只保留最好一轮：

1. 记录 `git rev-parse HEAD`、配置/规则摘要、系统/硬件、命令、开始/结束 UTC、退出码和原始日志。
2. 写清失败发生在哪一阶段、已提交边界、是否允许重试、补偿动作以及补偿后的对账结果。
3. 不通过时停止升级负载；不删除失败目录、不调整阈值、不以录屏替代仓库复现。
4. 断网、坏数据、kill 的恢复成功仅证明对应故障模型；不能外推断电、网络分区一致性或真实工业设备安全。

### 10.2 最终发布门禁

只有同一个最终候选 SHA 在 Ubuntu 与 RHEL 上完成 clean-clone、setup、离线 demo/verify、百万点、坏数据和 kill 演练，并完成 #56→#9 独立保留集后，才可在期末矩阵中把这些项标为“通过”。#136 容量 Gate 未完成时，查询 p95/EXPLAIN 和 100k 必须保留“未执行”，不得降级成文档完成。
## 11 用户查询容量门禁（Issue #136，Gate 0 计划）

> 本节属 #136（父单 #133，ADR-0007），与 #9 的「受控事件检出」是两个不同主题。负责人 B1（kittywent）承接，A 共同负责，C4 非作者复核，A 门禁。只授权 Gate 0（oracle / 可信计数 / D0 / 低负载），不授权高负载；D1/D2 与十万级逐档重新授权。

### 11.1 边界与数据口径

- 数据口径（ADR-0007 §5）：D0=10 设备 × 100 窗口（正确性 / 量测可信）；D1=1,000 设备 × 3,906 窗口 = 3,906,000 行 = 1,000 个 run 库；D2=10,000 设备 × 3,906 窗口 = 39,060,000 行 = 10,000 个 run 库。禁止把 D1/D2 合成一个大库。
- 初版工具范围为 `tools/query_load/**`、容量测试、证据与本计划/报告。2026-11-12 A 已在 [#136](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/136) 登记窄范围授权：`src/g15/service/http.py` 仅限 interface §16 的执行起止、原发时钟/PID、拒绝及响应交付终态；对应阶段集成回归与 interface/ADR 同步列入送审范围。原属地 B1 对2bcd639的[旧批准](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/157#pullrequestreview-5869062668)和对0632e32的[批准](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/157#pullrequestreview-5869226274)分别绑定各自提交；两张历史票后续均已DISMISSED，不能当作新head批准，也不将维护者登记当作非作者批准。`scripts/**`、部署、#56/#9保留集、其他产品模块保持原边界；新增修订仍须按精确SHA真实复核。
- 负载沿公开 HTTP 入口执行真实认证、授权与 SQLite。EXPLAIN 只读探针绑定实际内部 SELECT helper 的源码位置与摘要，不能冒充完整请求追踪，后续 helper 变更须复核探针。

### 11.2 可信计数口径（ADR-0007 §3）

- 每请求同一 request/trace ID 记录 `client_started / client_waiting / client_completed` 与 `server_received / server_queued / server_executing / server_completed`（口径定义；实际可用阶段见 §11.3）。
- `client_in_flight = started - completed`，`server_in_flight = received - completed`。
- oracle 独立读已提交 SQLite 逐项对照，不 mock 认证 / 授权 / repository / SQLite，不读 `private_truth`。
- 禁止 handler sleep / 人工延迟 / 网络限速制造在途峰值。

### 11.3 已知缺口（文档化，不伪造）

当前候选传输层增加配对 `server_executing / server_execution_completed`，逐请求保留原发纳秒单调时钟及 PID，执行区间不含响应发送。Expect/非 GET 明确 execution_not_started；过载只发 rejected 并记录读头/交付状态，不补造 received/completed。原始时钟缺失、事件漏失/重复/错序必须失败。当前无应用队列，queued 保持 null；记为 N/A 仍待非作者复核及 A 裁定。

窗口与告警 cursor 独立，但协议不能表达“这一流已耗尽”；省略该 cursor 会重发首页。工具保留每页原始响应并先做 SQLite oracle，再从副本中丢弃已耗尽流；分别报告原始/消费/丢弃数量。D0 的原始告警重发不等于服务端无重复，消费后覆盖通过不能代替原始无重复结论。ADR-0007 的“无重复错序”验收要求保持不变。

原 driver 保留同进程诊断入口；新 process_runner 使用独立客户端/服务端进程，分别采集原始资源样本、区间 CPU 差值和采样 RSS/FD/handle 最大值。采样最大值不等于区间精确峰值，进程启动以来的 RSS 峰值另列。双 Linux、完整 Gate 0 与更高档位仍需固定 SHA 的独立证据及 Review。

### 11.4 Gate 0 六步（#136）

1. harness / oracle / 计数定义 / 失败留存 / 停止条件 Review；
2. D0 逐请求验证响应 / 授权 / 分页 / 版本 / 日志；
3. 1 用户 / 10 请求 → 10 / 50 实际在途低负载；
4. 对账 client / server / request-log 证明计数可信；
5. Windows 开发回归 + 两 Linux 低负载兼容（不改 C6 脚本）；
6. 固定 SHA 留命令 / 退出码 / 机器规格 / 延迟分布 / 资源指标 / 失败。

### 11.5 测试层级

| 层级 | 位置 | 命令 |
|---|---|---|
| 集成（oracle + 计数） | `tests/integration/test_query_capacity_accounting.py` | `python -m pytest -q tests/integration/test_query_capacity_accounting.py` |
| 性能（opt-in） | `tests/performance/query/test_query_load.py` | `G15_RUN_QUERY_LOAD=1 python -m pytest -q -s tests/performance/query/test_query_load.py` |

harness 在 `tools/query_load/`：`d0_fixture.py`（D0 建库）、`accounting.py`（可信计数）、`oracle.py`（独立对账）、`client.py`（真实 HTTP 客户端）、`driver.py`（串行 + 并发 + 翻页全链驱动）。实测数字见 `docs/test_report.md` §10。

### 11.6 A 接管后的验证与证据入口（2026-11-12，#136）

- CLI、集成与 opt-in 共用 `runner.run_measurement` / `validate_run`：原始 oracle、消费后完整分页覆盖、计数守恒、应用日志三方对账分别给结论。`paging.py` 留原始与消费视图，`evidence.py` 保存每轮 `report.json`；已有目录拒绝覆盖，失败轮次也留档。
- 每份待核原始响应须与客户端记录逐条关联：request_id 多重集、状态、run/device 与实际收到的原始 JSON 内容一致；删响应、重复补数或事后改写都使验证失败。服务端的未知 ID 事件（含拒绝）不能遗漏；200 请求必须对应 received/received → executing/started → execution_completed/success → response_completed/success。
- `python -m tools.query_load.driver --concurrent 10 --output <新目录>`；50 波次使用 `--concurrent 50`。串行最多 10、并发只允许 10/50、workers 最多 64；发送前 barrier 仅同步起跑，不人为延迟请求/处理/响应。每个并发客户端有独立已认证且获授权的合成主体，应用 actor_ref 与客户端标签逐请求核对。
- 提交请求数与实测峰值分列：在途按高精度单调 `perf_counter` 起止区间计算，同时报告峰值时刻不同用户数、全程不同用户数与无法关联数量。目标要求客户端、服务端实际请求与用户峰值都达到该档；不足如实退出 1，不能拿 submitted=50 代替 server=50。基础检查与目标均满足时退出 0，仍不自动宣告完整 Gate 0。
- 证据包括源码 SHA/dirty/内容摘要、环境、命令模板、全部原始响应和失败、应用/传输日志、每请求原始延迟与 nearest-rank p50/p95/p99、资源点快照。EXPLAIN 调用实际 application SELECT helpers，记录 SQL/参数/源码行/hash；这是独立探针，不是整个 HTTP 路径追踪，D0 告警续页只是 SQL 分支探针。
- 负例覆盖重复页、缺页、等量替换、乱序、告警重复/缺失/错键，以及仅损坏将被丢弃的原始告警；单项 oracle 正确也不能掩盖跨页重复/缺失。异常 JSON/超时等仍完成客户端记账，ID 重复/错配/完成缺失必须失败。
- 每页独立检查键类型、唯一性与降序；凡本次请求省略某流 cursor，均按独立 SQLite 首屏键序核验该流，包括已耗尽后被重发、随后会被客户端丢弃的内容。原始空页或页内重复不能由消费视图通过抵消。
- 存储、查询计划、资源采集、证据计算、验证或工作目录清理的普通异常，按阶段记录安全错误类型并使本轮失败；诊断计算失败仍保留已有客户端/服务端记录与原始页。不会将 KeyboardInterrupt/SystemExit 改写为正常结果；目标目录写入错误仍向调用方报错。
- 产品阶段量测候选已按本轮扩展范围实现，契约见 interface §16；事件边界、失败与 queued 不适用裁定仍待 B1/C4 复核，不把 TCP backlog 当作应用排队数。

### 11.7 分进程低负载与契约反例（2026-11-12）

- `python -m tools.query_load.process_runner --concurrent 10 --output <新目录>`；50 使用另一新目录，默认 serial=10、workers=64。真实 TCP 预连接后只在客户端发送前 barrier 集合，每个主体发送一条 page_size=100 的合法查询，读取 D0 全部100窗口；服务器无 barrier、sleep、人工延迟、限速或自动重试。连接建立与 barrier 等待另留记录，不计为请求开始或请求延迟。
- 10次串行请求和分页使用普通短连接；并发主波次使用预连接。原始延迟必须按阶段分列并注明计时基准，不能把混合样本冒充单一冷连接或并发主波次分位数。客户端进程资源含控制、校验及分页 oracle 成本，服务端进程独立采样。
- oracle 必须独立核对实际持久化信封版本、返回窗口聚合版本、固定 query_contract_version、响应允许字段与严格 JSON 类型。对版本错/缺失、敏感审计字段、bool/float替代整数、nullable键缺失等实施真实 HTTP 损坏注入；measurement、CLI、opt-in 的共享验证必须判失败并留存证据。
- 执行阶段回归覆盖成功、401/404/400/405、查询异常、Expect、过载、socket写失败与真实断连。计数负例覆盖执行事件缺失/重复/错序、未知 ID、原发时钟和 PID 损坏；导入后的服务端区间不得按客户端收到事件的时刻重打时间。

### 11.8 每档一分钟观察与连接网络证据（2026-11-12 候选）

ADR §8.3的每档一分钟不能由单个短波次替代。新增显式 opt-in：`python -m tools.query_load.observation_runner --concurrent 10 --seconds 60 --output <库外新目录>`，10档质量与资源通过后，50档使用另一新目录。同一服务端进程持续存活，先执行1用户/10请求与真实page10完整遍历，再连续执行真实page100波次；每波有N个不同已认证授权主体，波间沿用同一组身份，仍不超过50并发。

本候选将时长定义为各质量通过并发波次的真实首次发送至最后完整读取并解码的区间之和至少60秒，同时报告墙钟与波次间空档；建连、barrier、校验、落盘时间不填入有效负载时长。各波峰值与达标数全部保留，汇总同时列min/max、达标波数及all_waves_target_reached，不能挑最好一波或外推为全时段恒定在途。任何质量/对账失败停止；时长未满、预算触顶或任一波未达目标均如实失败。此统计口径与最终阶段判据仍须非作者复核。

每波原始客户端记录仅保存一次base64响应；传输/应用/资源/网络数据流写入各波raw.ndjson，普通report剥离重复records/samples/connections；index.ndjson记录每波报告与原始文件大小、SHA-256、时长和结果。内存只保留当前波；默认最多10000波、总证据1GiB、每波预留16MiB，达到上界即停止并保存失败。中断先完成当前波失败证据与索引，再原样传播，不伪装正常完成。CLI拒绝不足60秒；短参数仅用于自动回归，不能取得正式一分钟标志。

socket网络观测显式包装真实连接，包含HTTP头和正文的应用流收发字节，按反向local/peer四元组逐连接对账并关联request_id；网络计数证据不记录payload/token。部分失败字节保持未知，中断、未真实关闭或错误记录不能被reset抹除。原发网络数据流必须与控制快照一致。此口径排除TCP/IP/链路头、重传和物理链路容量，不把socket应用流、正文长度或进程I/O混称wire容量或链路余量。

Windows、Ubuntu24.04、RHEL9须在同一固定实现SHA各自运行并分开署环境。Linux分别fresh clone、核锁文件Git原始blob、pp03/Python3.12与逐包版本，所有测试、缓存、原始观察目录均在笔记库外；只把普通可读摘要/索引/日志留入规划库，完整较大raw证据保留在库外目录并记录哈希与位置。
