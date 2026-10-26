# 测试报告：独立验证与报告（Issue #9）

> 负责人：B1（kittywent，2026-10-25 承接原 C5 职责）　审核人：A
> 计划见 `docs/test_plan.md`；口径依据 `docs/acceptance.md` §3/§4、`docs/interface.md` §5.3/§5.4/§8.5。
> **本报告只写真实跑过的结果。** 未跑的项在 §7 明列为未覆盖。

## 1 结论摘要

| 项 | 结果 |
|---|---|
| 受控事件检出 | **6 / 6（TP=6，FN=0，detection_rate=1.0）** |
| 正常场景误报 | **0**（两个 seed 各 0） |
| 退化场景判定 | **通过**（两 seed 后半段中位数均低于前半段） |
| 三类独立故障注入 | 业务故障 ✔　文件损坏/SHA ✔　Linux `kill -9` ⏳（Windows 上 skip，需 Linux 留证） |
| Issue 验收命令 | `pytest -q tests/integration tests/fault_injection` → **58 passed, 1 skipped，退出码 0** |
| 工程门禁 | black ✔　pylint **9.91/10** ✔　全量 pytest **578 passed, 9 skipped** ✔ |
| 双 Linux 实测 | **未做**（Issue #10 / C6），本报告数据全部来自 Windows |

## 2 被测身份与环境

| 项 | 值 |
|---|---|
| 基线提交 | `75b119454a1ac075620bdcc74826730fa7b4ac02` |
| `configs/health_rules.yaml` SHA-256（工作树 CRLF 字节） | `c26bad78809424599a48ccebd7f92b1b290e9d598a0367778127c9e1b9c77f1f` |
| `configs/health_rules.yaml` SHA-256（Git LF blob 字节） | `2b3a2763c72e32abd276b2d2617224b8bb941ca5d82807d2258db8d4c3f92d8b` |
| `configs/consumer.yaml` SHA-256（工作树 CRLF 字节） | `aebb18a9191f154c16e1320a73d7593774879d44573d5c10811fea8e35c08bac` |
| `configs/consumer.yaml` SHA-256（Git LF blob 字节） | `8006460052a9b741c1595ec94e5c487e99e54931315bc87482c9b197a5adde8c` |
| `configs/benchmark_1m.yaml` SHA-256（工作树 CRLF 字节） | `47ceeee975bae5ef0a47dddcb00b4468fb74430fcd6e5cee5a906640e0b4c1ab` |
| `configs/benchmark_1m.yaml` SHA-256（Git LF blob 字节） | `fa7ad03a153446707fd4aa49c6bccb5915a578eff471d6477d577c36c44ebd05` |
| `scoring_version` / `rule_id` | `heuristic-v1` / `health_heuristic` |
| 操作系统 | Windows 11 `10.0.26100`（AMD64） |
| Python | 3.12.15 (CPython)，项目 `.venv` |
| NumPy | 2.5.4 |
| 测量时间 | 2026-10-25（本机，非双 Linux） |

> **字节口径**：两列分别是「本机工作树按 CRLF 检出后的字节」与「Git 仓库内 LF blob 的字节」。差异只来自换行符，不是配置内容改动；A 已核收（Issue #56 评论 `<!-- A-T83-config-receipt -->`）。原 CRLF 摘要保留，不把换行差异写成业务改动。

被测链路：`load_config` → `produce_run_detailed` → `consume_run` → SQLite → `build_report`。
全部用例都跑真实链路，没有手工拼 NPZ、没有 fake 模块。

> **证据性质**：本轮全部结果只作**开发/回归证据**，不是独立保留集验收。seed=42 与 seed=20261016 在开发期已暴露，按 Issue #56 第 1 条保留原性质；本轮未替换 seed、未查看任何新的保留数据、未改任何阈值或门槛（#56 第 3/4 条）。正式独立验收须由 A 组织非作者复核后再定。

## 3 受控场景结果（10 个 run，逐条实测）

固定轮廓：单设备、100 Hz、`window_points=256`、`chunk_points=10240`、`total_points=65536`、`incomplete_window=drop`。

| 配置 | 类型 | 窗口 | 告警 | 误报 | 冗余 | 未匹配 | 事件 | 检出 | 检出率 | 生产计时 s | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `acceptance_healthy_seed42` | normal | 256 | 0 | 0 | 0 | 0 | 0 | 0 | `null` | 0.0976 | 通过 |
| `acceptance_healthy_seed20261016` | normal | 256 | 0 | 0 | 0 | 0 | 0 | 0 | `null` | 0.1131 | 通过 |
| `acceptance_degradation_seed42` | degradation | 256 | 1 | 1 | 0 | 1 | 0 | 0 | `null` | 0.1133 | 通过 |
| `acceptance_degradation_seed20261016` | degradation | 256 | 1 | 1 | 0 | 1 | 0 | 0 | `null` | 0.0905 | 通过 |
| `acceptance_spike_seed42` | fault | 256 | 1 | 0 | 0 | 0 | 1 | 1 | **1.0** | 0.1355 | 通过 |
| `acceptance_spike_seed20261016` | fault | 256 | 1 | 0 | 0 | 0 | 1 | 1 | **1.0** | 0.0857 | 通过 |
| `acceptance_step_seed42` | fault | 256 | 1 | 0 | 0 | 0 | 1 | 1 | **1.0** | 0.0866 | 通过 |
| `acceptance_step_seed20261016` | fault | 256 | 1 | 0 | 0 | 0 | 1 | 1 | **1.0** | 0.0889 | 通过 |
| `acceptance_burst_seed42` | fault | 256 | 1 | 0 | 0 | 0 | 1 | 1 | **1.0** | 0.0908 | 通过 |
| `acceptance_burst_seed20261016` | fault | 256 | 1 | 0 | 0 | 0 | 1 | 1 | **1.0** | 0.0897 | 通过 |

每个 run 都同时产出了 `reports/summary.json` 与 `reports/summary.md`（10/10）。

### 3.1 逐事件匹配明细（宽限 2 窗口）

| 配置 | event_id | 波形 | 区间 `[s,e)` | 允许窗口 | 检出 | 匹配告警起点 | 数据时延（样本） |
|---|---|---|---|---|---|---|---|
| spike_seed42 | F001 | spike | [16384, 16640) | [16384, 17152) | 是 | 16384 | −256 |
| spike_seed20261016 | F001 | spike | [16384, 16640) | [16384, 17152) | 是 | 16384 | −256 |
| step_seed42 | F001 | step | [16384, 18432) | [16384, 18944) | 是 | 16384 | −2048 |
| step_seed20261016 | F001 | step | [16384, 18432) | [16384, 18944) | 是 | 16384 | −2048 |
| burst_seed42 | F001 | burst | [16384, 18432) | [16384, 18944) | 是 | 16640 | −1792 |
| burst_seed20261016 | F001 | burst | [16384, 18432) | [16384, 18944) | 是 | 16640 | −1792 |

> 时延为负表示告警窗口起点早于事件结束（窗口与事件相交即算覆盖）。burst 的告警比 spike/step 晚一个窗口，说明该波形要累积到一个完整窗口才越过阈值——这是波形特性，不是匹配放宽。

### 3.2 退化场景趋势

| 配置 | 前半段中位数 | 后半段中位数 | 健康分最小 | 健康分最大 | 判定理由（原文） |
|---|---|---|---|---|---|
| degradation_seed42 | 83.3715 | 50.9336 | 49.4349 | 99.5296 | `second-half health median must be below the first-half median` |
| degradation_seed20261016 | 83.6415 | 50.7799 | 48.9787 | 99.8470 | 同上 |

退化场景各触发 1 条告警，按 §3 第 4 条**不计入**正常场景的 0 误报门槛；报告里仍单列为 `false_alarm_count=1`、`unmatched_alarm_count=1`，没有隐去。

## 4 三类独立故障注入证据（`docs/acceptance.md` §4）

### 4.1 业务故障（已完成）
见 §3：6/6 检出。**这一项不替代下面两类可靠性测试。**

### 4.2 文件损坏 / SHA 不匹配（已完成，5 个用例）

在真实运行产物上破坏，全部断言"拒收且未写入"：

| 用例 | 破坏方式 | 实测结果 |
|---|---|---|
| `test_corrupted_shard_bytes_are_refused_by_the_digest` | 分片中间一个字节取反 | `ConsumptionError`，消息含 `sha256 is a5e6... , manifest records 50ba...`；`feature_windows=0` |
| `test_missing_shard_is_refused` | 删除首个分片 | `ConsumptionError` / `OSError`；未写入 |
| `test_truncated_shard_is_refused` | 分片截断一半 | 拒收；未写入 |
| `test_partial_database_is_reusable_after_a_failed_shard` | 损坏 → 拒收 → 修复 → 重跑 | 重跑成功，`windows_written>0` 且库内窗口数等于写入数 |
| `test_second_consumption_is_idempotent` | 重复消费 | `windows_written=0`、`alarms_written=0`、`shards_skipped=shards_processed`，库内窗口数不变 |

### 4.3 `kill -9` 恢复（未完成，Windows 上 skip）
`test_kill_dash_nine_replay_matches_an_uninterrupted_run` 用真实 `SIGKILL` 打断子进程消费者，再重跑，并与未中断运行比对**库的全部内容**（`runs`/`processed_chunks`/`feature_windows`/`alarms`/`alarm_states` 五张表），不只比总行数。

用例本身做了三件事保证它到 Linux 上真的能证伪：①子进程进入 `consume_run` 后写握手文件，父进程轮询到握手才发信号，不用固定 `sleep`；②断言 `returncode == -SIGKILL`，子进程若自己跑完（返回 0）用例即失败，不会静默通过；③重跑后逐表比对内容，写对行数但写错内容的重放也会被抓到。

**在 Windows 上 skip**（`kill -9` 是 POSIX 语义，Windows 无等价物）；按 §4 必须在 Linux 上留证，本报告**不伪造**该项结果。

### 4.4 真值隔离（已完成，可证伪）

| 用例 | 操作 | 实测结果 |
|---|---|---|
| `test_validator_never_reads_the_truth_file_through_the_consumer` | 删除 `private_truth/` 后消费 | 消费成功，`alarm_count=1`、`fault_events=0`、`detection_rate=null`（消费者不依赖真值） |
| `test_damaged_truth_file_fails_the_report_not_the_consumer` | 消费成功后再写坏真值 | `build_report` 抛 `ReportError: invalid truth file`（真值只在报告层读） |
| `test_report_treats_a_missing_truth_file_as_unverifiable` | 故障 run 消费完成后删掉真值 | `verifiable=false`、`passed=null`、`detection_rate=null`，理由含 `private_truth`——缺文件不被读成"零事件" |
| `test_report_rejects_a_truth_file_from_another_run` | 把另一个 run 的真值拷进来 | `ReportError: belongs to run ...`，不允许张冠李戴 |
| `test_report_rejects_a_truth_file_with_broken_json` | 真值写成半截 JSON | `ReportError: invalid truth file`，JSON 语法错也走公开契约 |

## 5 工程门禁（真实输出）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `python -m pytest -q tests/integration tests/fault_injection` | 0 | **58 passed, 1 skipped in 7.26s** |
| `python -m pytest -q tests/unit` | 0 | **519 passed, 2 skipped in 5.52s** |
| `python -m pytest -q tests/integration` | 0 | **50 passed in 5.15s** |
| `python -m pytest -q tests/fault_injection` | 0 | **8 passed, 1 skipped in 3.09s** |
| `python -m pytest -q tests/performance` | 0 | **6 skipped**（opt-in，需显式开启） |
| `python -m pytest -q` | 0 | **578 passed, 9 skipped in 10.57s** |
| `python -m black --check src tests` | 0 | **47 files would be left unchanged** |
| `python -m pylint src/g15 --fail-under=8.5` | 0 | **9.91/10** |

9 条 skip 的构成：6 条性能 opt-in + 1 条 Linux `kill -9`（本机 Windows）+ 2 条符号链接权限用例。

## 6 用例规模汇总（`docs/acceptance.md` §4 的 ≥15 单测 / ≥20 用例）

| 目录 | 用例数 |
|---|---|
| `tests/unit/` | 521 |
| `tests/integration/` | 50（其中我新增 22 条匹配规则 + 28 条端到端报告） |
| `tests/fault_injection/` | 9（全部新增） |
| `tests/performance/` | 6 |
| `tests/test_package_import.py` | 1 |
| **合计** | **587** |

单元测试远超 15 条、可执行用例远超 20 条；本报告只统计**实际跑过**的（见 §5）。

## 6.1 复审整改（A 的 R1–R6，2026-10-26）

A 在 `5646201287` 上给了六条 CHANGES_REQUESTED。逐条整改与可证伪证据如下：

| 编号 | 问题 | 整改 | 证伪方式 |
|---|---|---|---|
| R1 | 报告默认 `W=256`，把配置化消费误判成 100% 检出 | 窗口宽度改为**从已存窗口几何读出**（`end_seq - start_seq`）；宽度不一致直接 `ReportError` | `test_report_uses_the_window_width_read_from_the_database`：真实 W=64、事件 `[0,64)`、告警在 256，允许区间 `[0,192)` → 判**未检出 + 1 误报**；若仍按 256 算，区间变 `[0,768)` 会误判成检出 |
| R2 | 缺真值被当成"零事件"，故障 run 变正常通过 | 缺文件时**只认日志 `run_complete.fault_events == 0`**；否则 `verifiable=false`、`passed=null`；真值 `run_id` 必须与 manifest 一致；JSON 语法错也走 `ReportError` | 删真值 → `verifiable=false`；拷别的 run 的真值 → `ReportError: belongs to run`；半截 JSON → `ReportError: invalid truth file` |
| R3 | 读另一个硬编码库的 `scoring_version`，空库也能通过 | 一律用**传入的 `database_path`**，按 `manifest.run_id` 查 `runs` 行；无该行即"未消费"，不可验 | `test_report_does_not_read_another_runs_database`（A 的库配 B 的 run → `passed=null`）；`test_report_reads_scoring_version_from_the_supplied_database`（改库内 `scoring_version='probe-v9'` → 报告读到同一值）；`test_report_survives_a_database_that_was_only_initialised`（空库 → `passed=null`） |
| R4 | `processed_points` 把整片 rows 当成分析覆盖 | 拆成 `processed_points`（已提交片覆盖）/ `analysed_points`（真实入窗）/ `dropped_points`（片内未成窗）/ `rejected_points`（未消费片），四项之和等于 `total_points` | `test_report_reports_analysed_and_dropped_points_separately`：单片 10240、W=7000 → `processed=10240, analysed=7000, dropped=3240, rejected=0`，和 = 10240 |
| R5 | 告警未去重，一条预测能检出两个事件；贪心反例不成立 | 按 `(device_id, rule_id, start_seq)` 去重并限定 `yellow/red`，被丢弃行计入 `dropped_alarm_count`；贪心反例改成真的反例；并列多解的指派写进断言 | `test_duplicate_alarm_keys_cannot_detect_two_events`、`test_duplicate_keys_on_different_levels_are_still_one_alarm`、`test_alarm_level_outside_the_contract_never_detects`、`test_maximum_matching_beats_greedy_when_a_later_event_has_no_alternative`（A 提的真实反例：A=`[0,1024)`、B=`[0,1)`、告警 0/768 → 最大 2）、`test_tie_break_between_identical_events_is_pinned` |
| R6 | `kill -9` 用例用固定 sleep、不查返回码、只比行数 | 改为握手文件 + 轮询；断言 `returncode == -SIGKILL`；重跑后逐表比对五张表全部内容 | 用例在 Windows 上 skip；到 Linux 上才真正执行。Windows 上**不伪造**该结果 |

另：`docs/test_plan.md` §6.3/§6.4 已同步成上述口径。

## 7 未覆盖与风险（不得在答辩中宣称）

1. **双 Linux 实测**：Ubuntu 24.04 / RHEL 9 的全新克隆、安装、运行、`kill -9` 留证属 Issue #10（C6），本报告全部数据来自 Windows，不能替代。
2. **`kill -9` 恢复**：本机无 POSIX 信号语义，未取得证据；须在 Linux 上补跑并回填本节。
3. **百万点 60 秒**：属生产基准（Issue #11 验收），本报告受控轮廓仅 65536 点。
4. **真实工业准确率**：本报告的"100%"只指 §3 预定义教学测试集上的事件召回率。
5. **去抖/升级/恢复（N>1）**：受控轮廓固定 N=1，未覆盖 N>1 边界。
6. **规则内容指纹**：`scoring_version` 变更会被拒收，但同版本改规则内容或改尾窗策略目前不会自动检查（#57 遗留 P2，C2/A 侧收口）。

## 8 复现命令

```bash
# Issue 验收
python -m pytest -q tests/integration tests/fault_injection

# 全量门禁
python -m pytest -q
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5

# 单跑一个受控场景并生成报告
python -c "
from pathlib import Path
from g15.config import load_config, load_consumer_config
from g15.pipeline.producer import produce_run_detailed
from g15.pipeline.consumer import consume_run
from g15.reporting.builder import build_report
cfg = load_config(Path('configs/acceptance_spike_seed42.yaml'))
s = produce_run_detailed(cfg, Path('runs'))
db = s.run_dir / 'artifacts' / 'results.sqlite'
consume_run(s.run_dir, db, load_consumer_config(Path('configs/consumer.yaml')))
print(build_report(s.run_dir, db))
"
```
