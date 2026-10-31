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
| Issue 验收命令 | `pytest -q tests/integration tests/fault_injection` → **60 passed, 1 skipped，退出码 0** |
| 工程门禁 | black ✔　pylint **9.91/10** ✔　全量 pytest **581 passed, 9 skipped** ✔ |
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

### 3.3 三场景闭环报告取证（2026-10-30，R8 改动后重跑）

`docs/acceptance.md` §4 与 `docs/interface.md` §14 R08 要的是**报告产出**，不是让演示配置判「通过」。用仓库自带演示配置各跑一次真实链路（`produce_run_detailed` → `consume_run` → `build_report`），每个 run 都同时产出 `reports/summary.json` 与 `reports/summary.md`：

| 配置 | `scenario_id` | 点数 | 窗口 | 告警 | `scenario_kind` | 类型来源 | `verifiable` | `passed` | `summary.md` |
|---|---|---|---|---|---|---|---|---|---|
| `healthy.yaml` | `healthy_001` | 200000 | 781 | 0 | `null` | `null` | `false` | `null` | 2703 B |
| `degradation.yaml` | `degradation_001` | 1000000 | 3906 | 7 | `null` | `null` | `false` | `null` | 2735 B |
| `sudden_fault.yaml` | `sudden_fault_001` | 1000000 | 3906 | 10 | `fault` | `fault_events` | `true` | `true` | 2773 B |

三条的 `verdict_reasons` 对前两条写的是「无法从 run 目录确定场景类型：scenario_id 'healthy_001' / 'degradation_001' 不在受审验收清单内，且 manifest 与日志均无类型声明」。这是预期结果：它们不是 §3 的受审验收配置，报告层没有证据判它们「通过」，也不该拿 id 猜。`sudden_fault_001` 有注入事件，按事件证据判 `fault`，检出 1/1、误报 9、`detection_rate=1.0`。

`benchmark_1m.yaml` 同样落 `scenario_kind=null`、`passed=null`（3906 窗口、9 条告警、`summary.md` 正常产出），它的验收口径是 §2 的百万点计时，不是事件检出。`bearing_001`（`default.yaml`）有 1 个注入事件，判 `fault`、检出 1/1、误报 9。

演示 id **没有**写进 `ACCEPTANCE_SCENARIO_KINDS`。把「报告产出了」当成「演示配置通过验收」是两回事，混起来等于自欺。

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

**握手时机的真实边界**（按 A 第三轮评审 `5648720609` 的限定）：监视线程轮询 `processed_chunks`，看到第 1 行即写握手；但轮询**不能精确保证**此时"第一片刚提交、正处理第二片"，消费者可能已提交更晚的片。因此实际断言的是**某个非空的部分前缀**（`1 <= 已提交片数 < manifest.parts` 总数），不是"恰好一片"。本报告不把该用例描述成能证明更精确的时序。

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
| `python -m pytest -q tests/integration tests/fault_injection` | 0 | **61 passed, 1 skipped in 6.90s** |
| `python -m pytest -q tests/unit` | 0 | **519 passed, 2 skipped in 3.97s** |
| `python -m pytest -q tests/integration` | 0 | **53 passed in 5.99s** |
| `python -m pytest -q tests/fault_injection` | 0 | **8 passed, 1 skipped in 2.00s** |
| `python -m pytest -q tests/performance` | 0 | **6 skipped**（opt-in，需显式开启） |
| `python -m pytest -q` | 0 | **581 passed, 9 skipped in 12.80s** |
| `python -m black --check src tests` | 0 | **47 files would be left unchanged** |
| `python -m pylint src/g15 --fail-under=8.5` | 0 | **9.91/10** |

9 条 skip 的构成：6 条性能 opt-in + 1 条 Linux `kill -9`（本机 Windows）+ 2 条符号链接权限用例。

> 上表是 §2 那个基线 `75b11945` 上的输出，保留原样不改。`#70`/`#72` 合入后主线换了，本节新增的下表是另一轮实测，两组数字不混用。

### 5.1 主线 `8eee3ca` 追加实测（#72 独立校验器，2026-10-31）

`main = 8eee3ca2fbe403873d842670c8561d62ce20817e`（#70 + #72 均已合入），Windows 11 / Python 3.12.15 / NumPy 2.5.4，干净 worktree。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `pytest -q` | 0 | **667 passed, 10 skipped in 16.67s** |
| `pytest -q tests/unit` | 0 | **541 passed, 2 skipped in 4.33s** |
| `pytest -q tests/integration` | 0 | **117 passed, 1 skipped in 8.83s** |
| `pytest -q tests/fault_injection` | 0 | **8 passed, 1 skipped in 3.57s** |
| `pytest -q tests/integration tests/fault_injection`（Issue 验收） | 0 | **125 passed, 2 skipped in 10.13s** |
| `pytest -q tests/integration/test_run_verifier.py` | 0 | **57 passed, 1 skipped in 1.81s** |
| `black --check src tests` | 0 | **51 files would be left unchanged** |
| `pylint src/g15 --fail-under=8.5` | 0 | **9.91/10** |

10 条 skip 的构成：6 条性能 opt-in + 1 条 Linux `kill -9` + 3 条符号链接权限用例（`test_run_verifier.py`、`test_doc_references.py`、`test_serialization.py` 各一条）。

**`verify_run` 真实探针**（每条都在真实产物上跑，脚本用完即删，未入库）：

| 场景 | 结果 |
|---|---|
| `acceptance_spike_seed42` 正常 run | `passed=True`，`rows_sum=65536`、`part_count=7`、`verified_part_count=7`、`scalar_value_count=131072`、`shard_shape=6x10240+4096`、`first_seq=0`/`last_seq=65535`，耗时 **0.0116 s** |
| 中间片翻转一个字节 | `passed=False`，`failures=["seq.cross_shard_gap","shard.hash_mismatch"]`，`verified_part_count=6/7` |
| 删除第 3 片 | `passed=False`，`failures=["seq.cross_shard_gap","shard.missing"]` |
| 删除 `private_truth/faults.json` 后校验 | `passed=True`，`failures=[]`——校验器不依赖真值 |
| `timestamp_ns` 整列置 0 并重算摘要 | `passed=True`，`failures=[]`——**这就是已知缺口**，见 §7 第 7 条 |
| `benchmark_1m` 百万点 run | `passed=True`，`rows_sum=1000000`、`part_count=10`、`scalar_value_count=2000000`、`shard_shape=9x102400+78400`；三次独立调用耗时 0.0721 / 0.0737 / 0.0710 s，中位数 **0.0721 s**、最大 **0.0737 s** |

百万点那一行是 **`verify_run` 自己的耗时**，不是生产窗口，也不是 CLI 总墙钟。同一 run 的生产计时窗口是 0.160 s（本机单次，非验收口径）。CLI 总墙钟归 C3 的 `benchmark` 子命令、进程峰值 RSS 归 C6 的进程外工具，本报告不代填。

### 5.2 压力测试实测（重复执行、并发与中断恢复，2026-10-31）

口径见 `docs/test_plan.md` §5.2。用例已入库为 `tests/integration/test_stress_repeatability.py`（4 个测试函数，`G15_RUN_STRESS=1` 才跑），不再是脚本。

**A. 自动化用例实跑（2026-10-31，Windows 11 / Python 3.12.15 / NumPy 2.5.4）**

```bash
G15_RUN_STRESS=1 python -m pytest -q -s tests/integration/test_stress_repeatability.py
```

连跑 4 轮，每轮 **4 passed**，无偶发失败：

| 用例 | 实测 |
|---|---|
| `test_repeated_production_lands_byte_identical_shards` | 3 次生产的 `(part_id, rows, sha256)` 全等；3 次耗时 0.091 / 0.084 / 0.085 s（另几轮 0.092/0.093/0.080、0.095/0.083/0.086；有一轮首项 0.743 s，是冷启动抖动，只作记录不作判据） |
| `test_repeated_consumption_adds_no_row` | 首轮写入 256 窗口 / 1 告警；后两轮 `windows_written=0`、`alarms_written=0`、`shards_processed=0`、`shards_skipped=7`，四表行数不变 |
| `test_racing_consumers_never_duplicate_rows` | 3 个 spawn 消费者：典型结果是一个 `written/256` + 两个失败（`IntegrityError` 唯一键冲突或 `DatabaseError` 锁），也出现过两个成功（先跑完的写 256，后进来的写 0）；四轮最终库内容都等于单进程跑一遍的结果，无重复行 |
| `test_abruptly_stopped_consumption_replays_to_the_same_content` | `benchmark_1m` 10 分片，第 1 片事务提交后 kill，停在 `processed_chunks=1/10`；重放写 3506 窗口 / 9 告警、处理 9 片跳过 1 片，最终 `runs 1 / chunks 10 / windows 3906 / alarms 9`，四表全量行与未中断对照库逐行相同 |

**B. 默认门禁不受影响**：不设 `G15_RUN_STRESS` 时这 4 条全部 skip。本分支上实测 `pytest -q` = **667 passed, 14 skipped**（比主线多出的 4 条 skip 就是它们）；设了开关后全量是 **671 passed, 10 skipped**。

**补充：一次性探针的历史数字（2026-10-31 早前，未入库）**

下面三张表来自 PR #84 的探针，保留作对照；它们**不是**上面 4 条用例的输出，两条证据不能互相替代。

**C1. 受控轮廓 `acceptance_spike_seed42` 连跑 10 轮**（每轮独立 run 目录 + 独立库）：

| 阶段 | 中位数 | 最大 | 最小 |
|---|---|---|---|
| 生产 | 0.034 s | 0.039 s | 0.032 s |
| 消费 | 0.058 s | 0.065 s | 0.055 s |
| 报告 | 0.003 s | 0.068 s（首轮，含冷启动） | 0.002 s |
| 全链 | 0.095 s | 0.162 s（首轮） | 0.091 s |

10 轮全部 `passed=True`、`detection_rate=1.0`、`false_alarm_count=0`，无跨轮漂移。

**C2. 同一 run 连续重放 5 次到同一个库**：耗时 0.055 / 0.011 / 0.011 / 0.011 / 0.011 s（首轮建表后趋稳），`feature_windows` 恒为 **256**、`alarms` 恒为 **1**——重放幂等，行数不增长。

**C3. 百万点轮廓 `benchmark_1m` 串跑 3 轮**（生产 → 消费 → 报告 → 校验）：

| 阶段 | 中位数 | 最大 |
|---|---|---|
| 生产 | 0.157 s | 0.254 s |
| 消费 | 0.357 s | 0.361 s |
| 报告 | 0.012 s | 0.061 s |
| 校验 `verify_run` | 0.053 s | 0.054 s |
| 全链 | 0.617 s | 0.672 s |

3 轮的 `processed_points` 都是 1000000，`verifiable=False` / `passed=null` 也每轮一致——`benchmark_1m` 的 `events` 为空且 id 不在受审验收清单内，按 R8 判据本就不能判通过，这是**判据正确**而不是失败。压力面只关心三轮是否一致，不关心结论是不是通过。

**这些是 Windows 单机数字，不是双 Linux 验收数据，也不进 §5 的 60 秒口径**——它们只是重复执行下的稳定性证据。

## 6 用例规模汇总（`docs/acceptance.md` §4 的 ≥15 单测 / ≥20 用例）

| 目录 | 基线 `75b11945` 收集数 | 主线 `8eee3ca` 收集数 | 本分支（含压力用例） |
|---|---|---|---|
| `tests/unit/` | 521 | **543** | **543** |
| `tests/integration/` | 53（其中我新增 22 条匹配规则 + 31 条端到端报告） | **118**（`test_run_verifier.py` 58 项为 #72 新增） | **122**（`test_stress_repeatability.py` 4 条为 #51 新增） |
| `tests/fault_injection/` | 9（全部新增） | **9** | **9** |
| `tests/performance/` | 6 | **6** | **6** |
| `tests/test_package_import.py` | 1 | **1** | **1** |
| **合计** | **590** | **677** | **681** |

单元测试远超 15 条、可执行用例远超 20 条；本报告只统计**实际跑过**的（见 §5 与 §5.1）。三列都是 `pytest --collect-only` 的收集数，不是"通过数"。压力用例默认 skip，所以本分支"跑过"的是 667 条，4 条压力用例要显式开 `G15_RUN_STRESS=1` 才计入。

## 6.1 复审整改（A 的 R1–R7，2026-10-26）

A 在 `5646201287` 上给了六条 CHANGES_REQUESTED（R1–R6），第三轮 `5648720609` 追加 R7。逐条整改与可证伪证据如下（R2/R3/R6 含第二轮追加）：

| 编号 | 问题 | 整改 | 证伪方式 |
|---|---|---|---|
| R1 | 报告默认 `W=256`，把配置化消费误判成 100% 检出 | 窗口宽度改为**从已存窗口几何读出**（`end_seq - start_seq`）；宽度不一致直接 `ReportError` | `test_report_uses_the_window_width_read_from_the_database`：真实 W=64、事件 `[0,64)`、告警在 256，允许区间 `[0,192)` → 判**未检出 + 1 误报**；若仍按 256 算，区间变 `[0,768)` 会误判成检出 |
| R2 | 缺真值被当成"零事件"，故障 run 变正常通过 | 缺文件时**只认日志 `run_complete.fault_events == 0`**；否则 `verifiable=false`、`passed=null`；真值 `run_id` 必须与 manifest 一致；JSON 语法错也走 `ReportError`。**第二轮追加**：真值未知时 `fault_events`/`detected_events`/`missed_events`/`false_alarm_count`/`redundant_alarm_count`/`unmatched_alarm_count` 一律 `null`，不得把可能命中隐形事件的告警算成误报 | 删真值 → `verifiable=false`；拷别的 run 的真值 → `ReportError: belongs to run`；半截 JSON → `ReportError: invalid truth file`；`test_validator_never_reads_the_truth_file_through_the_consumer` 断言六个计数全 `null` |
| R3 | 读另一个硬编码库的 `scoring_version`，空库也能通过；**有 `runs` 行就当整 run 完成** | 一律用**传入的 `database_path`**，按 `manifest.run_id` 查 `runs` 行；**第二轮追加**：`manifest.parts` 每个分片都要有 `processed_chunks` 行且 `sha256` 与清单一致，缺行/不符即部分消费，`verifiable=false`、`passed=null`，并新增 `pending_parts`/`digest_mismatch_parts` 字段 | `test_report_does_not_read_another_runs_database`（A 的库配 B 的 run → `passed=null`）；`test_report_reads_scoring_version_from_the_supplied_database`；`test_report_survives_a_database_that_was_only_initialised`；**`test_report_refuses_a_partially_consumed_run`**（真实生产 65536 点 → 损坏第 2 片 → 消费在第一片提交后失败 → 报告 `pending_parts=[1..6]`、`processed=10240`、`rejected=55296`、`passed=null`；修好重跑 → `passed=true`、`pending=[]`）；**`test_report_refuses_a_run_whose_recorded_digest_was_swapped`**（改 `processed_chunks.sha256` → `digest_mismatch_parts=[3]`、`passed=null`） |
| R4 | `processed_points` 把整片 rows 当成分析覆盖 | 拆成 `processed_points`（已提交片覆盖）/ `analysed_points`（真实入窗）/ `dropped_points`（片内未成窗）/ `rejected_points`（未消费片）。四个字段不构成一条和式，而是两条恒等式：`processed + rejected = total`（分片口径），`analysed + dropped = processed`（分析口径） | `test_report_reports_analysed_and_dropped_points_separately`：单片 10240、W=7000 → `processed=10240, analysed=7000, dropped=3240, rejected=0`，两式分别为 `10240+0=10240` 与 `7000+3240=10240` |
| R5 | 告警未去重，一条预测能检出两个事件；贪心反例不成立 | 按 `(device_id, rule_id, start_seq)` 去重并限定 `yellow/red`，被丢弃行计入 `dropped_alarm_count`；贪心反例改成真的反例；并列多解的指派写进断言 | `test_duplicate_alarm_keys_cannot_detect_two_events`、`test_duplicate_keys_on_different_levels_are_still_one_alarm`、`test_alarm_level_outside_the_contract_never_detects`、`test_maximum_matching_beats_greedy_when_a_later_event_has_no_alternative`（A 提的真实反例：A=`[0,1024)`、B=`[0,1)`、告警 0/768 → 最大 2）、`test_tie_break_between_identical_events_is_pinned` |
| R6 | `kill -9` 用例用固定 sleep、不查返回码、只比行数；**握手点在 `consume_run` 之前** | 改为**子进程监视线程轮询 `processed_chunks`**：第一片事务提交后写握手；断言 `returncode == -SIGKILL`；信号后先断言 `1 <= len(processed_chunks) < manifest.parts` 证明是真实中间态；重跑后逐表比对五张表全部内容。**边界限定**（A `5648720609`）：轮询不能精确保证"刚提交第一片、正处理第二片"，实际观察到的是某个**非空部分前缀**，文档按此表述，不声称更精确时序 | 用例在 Windows 上 skip；到 Linux 上才真正执行。Windows 上**不伪造**该结果。握手靠观察数据库取得，**未给业务代码加任何测试专用参数** |
| R7 | 摘要冲突时报告混用两套口径：`processed_points` 扣掉冲突片，`analysed_points` 却仍统计该片窗口 → **负丢弃点**（`-10240`），且检出率仍来自含无效片的窗口/告警 | **统一有效数据范围**：新增 `_part_ranges`/`_contains`，按 manifest 累加 rows 得每片 seq 区间；窗口与告警只在**有效片**（已提交且摘要一致）内计入 `analysed_points`/`alarm_count`/匹配指标。无法归属的行**不静默丢弃**，另列 `raw_feature_windows`/`excluded_feature_windows`/`raw_alarm_count`/`excluded_alarm_count`，把"库中原始记录"与"可归属当前 manifest 的数据"分开；不使用 `max(0, dropped)` 掩盖范围不一致 | `test_report_refuses_a_run_whose_recorded_digest_was_swapped` 扩充：断言 `dropped_points >= 0`、`analysed == processed`、两式恒等、`excluded_feature_windows > 0`、`feature_windows + excluded_feature_windows == raw_feature_windows`、`excluded_alarm_count == raw_alarm_count - alarm_count`、Markdown 与 JSON 同源且不含 `-10240`。新增 `test_report_excludes_alarms_from_a_shard_whose_digest_was_swapped`：往 part 3 插一条告警（`raw_alarm_count=2`、`false_alarm_count=1`）→ 置换 part 3 摘要后 `excluded_alarm_count=1`、`alarm_count=1`、`false_alarm_count=0`，Markdown 同步。**变异测试**：把窗口归属改回"全部计入" → `assert -10240 >= 0` 失败；把告警归属改回"全部保留" → `assert 0 == 1` 失败；两处恢复后均通过 |

另：`docs/test_plan.md` §6.3/§6.4 已同步成上述口径。

## 7 未覆盖与风险（不得在答辩中宣称）

1. **双 Linux 实测**：Ubuntu 24.04 / RHEL 9 的全新克隆、安装、运行、`kill -9` 留证属 Issue #10（C6），本报告全部数据来自 Windows，不能替代。
2. **`kill -9` 恢复**：本机无 POSIX 信号语义，未取得证据；须在 Linux 上补跑并回填本节。
3. **百万点 60 秒**：属生产基准（Issue #11 验收），本报告受控轮廓仅 65536 点。
4. **真实工业准确率**：本报告的"100%"只指 §3 预定义教学测试集上的事件召回率。
5. **去抖/升级/恢复（N>1）**：受控轮廓固定 N=1，未覆盖 N>1 边界。
6. **规则内容指纹**：`scoring_version` 变更会被拒收，但同版本改规则内容或改尾窗策略目前不会自动检查（#57 遗留 P2，C2/A 侧收口）。
7. **`verify_run` 不校验时间轴**：七项 checks 不含 `timestamp_ns` 的绝对时间公式（`start + seq / sample_rate`）。`timestamp_ns` 整列置 0 或倒序、摘要重算后仍 `passed=True`，已实测（§5.1）。原始分片完整性通过不等于时间轴核验，不得在答辩中说"产物全部核验通过"。
8. **压力测试的边界**：§5.2 已覆盖重复生产、重放幂等、3 进程争用同一 SQLite、以及本平台上的中途终止后重放。仍**没有** Linux `kill -9` 用例（归 #10）、没有长时间浸泡、没有并发度梯度（只固定 3 个），也**不**把并发消费写成受支持能力——3 个消费者里通常只有 1 个成功，其余按唯一键冲突或锁超时被拒。不得拿这一节当"并发能力已验证"的证据。

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
