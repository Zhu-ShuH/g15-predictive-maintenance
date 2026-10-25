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
| Issue 验收命令 | `pytest -q tests/integration tests/fault_injection` → **46 passed, 1 skipped，退出码 0** |
| 工程门禁 | black ✔　pylint **9.92/10** ✔　全量 pytest **566 passed, 9 skipped** ✔ |
| 双 Linux 实测 | **未做**（Issue #10 / C6），本报告数据全部来自 Windows |

## 2 被测身份与环境

| 项 | 值 |
|---|---|
| 基线提交 | `75b119454a1ac075620bdcc74826730fa7b4ac02` |
| `configs/health_rules.yaml` SHA-256 | `c26bad78809424599a48ccebd7f92b1b290e9d598a0367778127c9e1b9c77f1f` |
| `configs/consumer.yaml` SHA-256 | `aebb18a9191f154c16e1320a73d7593774879d44573d5c10811fea8e35c08bac` |
| `configs/benchmark_1m.yaml` SHA-256 | `47ceeee975bae5ef0a47dddcb00b4468fb74430fcd6e5cee5a906640e0b4c1ab` |
| `scoring_version` / `rule_id` | `heuristic-v1` / `health_heuristic` |
| 操作系统 | Windows 11 `10.0.26100`（AMD64） |
| Python | 3.12.15 (CPython)，项目 `.venv` |
| NumPy | 2.5.4 |
| 测量时间 | 2026-10-25（本机，非双 Linux） |

被测链路：`load_config` → `produce_run_detailed` → `consume_run` → SQLite → `build_report`。
全部用例都跑真实链路，没有手工拼 NPZ、没有 fake 模块。

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
`test_kill_dash_nine_replay_matches_an_uninterrupted_run` 用真实 `SIGKILL` 打断子进程消费者，再重跑并与未中断运行比对窗口数。
**在 Windows 上 skip**（`kill -9` 是 POSIX 语义，Windows 无等价物）；按 §4 必须在 Linux 上留证，本报告**不伪造**该项结果。

### 4.4 真值隔离（已完成，可证伪）

| 用例 | 操作 | 实测结果 |
|---|---|---|
| `test_validator_never_reads_the_truth_file_through_the_consumer` | 删除 `private_truth/` 后消费 | 消费成功，`alarm_count=1`、`fault_events=0`、`detection_rate=null`（消费者不依赖真值） |
| `test_damaged_truth_file_fails_the_report_not_the_consumer` | 消费成功后再写坏真值 | `build_report` 抛 `ReportError: invalid truth file`（真值只在报告层读） |

## 5 工程门禁（真实输出）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `python -m pytest -q tests/integration tests/fault_injection` | 0 | **46 passed, 1 skipped in 4.94s** |
| `python -m pytest -q tests/unit` | 0 | **519 passed, 2 skipped in 4.58s** |
| `python -m pytest -q tests/integration` | 0 | **38 passed in 4.50s** |
| `python -m pytest -q tests/fault_injection` | 0 | **8 passed, 1 skipped in 1.70s** |
| `python -m pytest -q tests/performance` | 0 | **6 skipped**（opt-in，需显式开启） |
| `python -m pytest -q` | 0 | **566 passed, 9 skipped in 10.05s** |
| `python -m black --check src tests` | 0 | **47 files would be left unchanged** |
| `python -m pylint src/g15 --fail-under=8.5` | 0 | **9.92/10**（previous 9.91） |

9 条 skip 的构成：6 条性能 opt-in + 1 条 Linux `kill -9`（本机 Windows）+ 2 条符号链接权限用例。

## 6 用例规模汇总（`docs/acceptance.md` §4 的 ≥15 单测 / ≥20 用例）

| 目录 | 用例数 |
|---|---|
| `tests/unit/` | 521 |
| `tests/integration/` | 38（其中我新增 22 条匹配规则 + 16 条端到端报告） |
| `tests/fault_injection/` | 9（全部新增） |
| `tests/performance/` | 6 |
| `tests/test_package_import.py` | 1 |
| **合计** | **575** |

单元测试远超 15 条、可执行用例远超 20 条；本报告只统计**实际跑过**的（见 §5）。

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
