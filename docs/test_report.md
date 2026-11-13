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
| 三类独立故障注入 | 业务故障 ✔　文件损坏/SHA ✔　Linux `kill -9` ✔（C6 于 #51 在 Ubuntu 24.04.4 与 RHEL 9.7 各跑出 9 passed、无 skip；本机 Windows 仍按平台 skip，留证与边界见 §4.3） |
| Issue 验收命令 | `pytest -q tests/integration tests/fault_injection` → **155 passed, 6 skipped，退出码 0**（main `195f5aa`，2026-11-04 复跑，与 `2054bb5` 一致） |
| 工程门禁 | black ✔（53 files）　pylint **9.91/10** ✔　全量 pytest **755 passed, 14 skipped** ✔（main `195f5aa`） |
| 双 Linux 实测 | `kill -9`（§4.3）、百万点基准（§5.3 A 表）、公开仓库全新克隆已有 Ubuntu 24.04.4 / RHEL 9.7 留证；主链接通后的 `545a598c` 还在两机完成 setup→clean→demo→verify→benchmark（均退出 0，#10 评论 `6759021839`）。**仍未完成**的是 #56/#9 独立保留集检出率的双系统复现及最终候选 SHA 整包复证 |

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

### 3.4 场景类型声明链路的验证（Issue #83）

manifest 升到 2.0 并新增可选键 `scenario_kind` 之后，这条链路按层验证，全部用例在 `pytest -q` 里跑：

| 层 | 验证点 | 用例 |
|---|---|---|
| 配置 | `scenario.kind` 取 `normal`/`degradation` 合法；`fault`、拼错、`null`、非字符串被点名拒收 | `tests/unit/test_config.py` 5 条 |
| 序列化 | 2.0 + 声明往返保留；1.0 无键仍可读；1.0 带新键仍可读；3.0 在键集校验前报版本错；显式 `null`/非法值/非字符串被拒；缺 `schema_version` 时报出真实缺失键与未知键 | `tests/unit/test_serialization.py` 8 条 |
| 生产 | 声明写进 manifest 且版本为 2.0；未声明时不写该键；有注入事件推出 `fault`；声明与注入冲突时 `ValueError` 且不落盘 | `tests/unit/test_producer.py` 6 条 |
| 报告 | manifest 声明可分类（来源 `manifest`）；声明与清单一致不改结论；声明 `normal` 而真值有事件记为 blocker（报出的 kind/source 仍取证据）；声明 `fault` 而真值零事件记为 blocker 且不归因检测率 | `tests/integration/test_validation_report.py` 4 条 |

版本窗口是读入侧的事：`{1.0, 2.0}` 之外的版本在键集校验之前拒收，所以 3.0 的 run 拿到的是版本错误而不是「缺键」，避免把「读不了」误报成「数据坏了」。`scenario_kind` 缺键表示未声明，报告层回退到受审验收清单并记 `acceptance_manifest`；显式 `null` 不是「未声明」的表示法，会被拒收。

本分支（`feat/83-manifest-scenario-kind`，base `6375e2e`）实测：全量 `pytest -q` = **784 passed, 14 skipped in 24.84 s**，`--collect-only` = 798；收集数由 base 的 766 升到 798，其中上表四层是 23 个新用例函数（配置 5 / 序列化 8 / 生产 6 / 报告 4），其余 9 条差额来自既有用例的参数化展开；`black --check src tests` = 53 files unchanged；`pylint src/g15 --fail-under=8.5` = **9.92/10**，base `6375e2e` 隔离复测为 9.91/10（+0.01）；本轮没有新增告警，且 base 的一条 R0912 too-many-branches (14/12) 被消除（kind 判定段抽成 `_kind_and_source` / `_kind_blockers` 后分支数回落阈值内），其余告警逐条一致。

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

### 4.3 `kill -9` 恢复（Linux 已留证；Windows 上 skip）
`test_kill_dash_nine_replay_matches_an_uninterrupted_run` 用真实 `SIGKILL` 打断子进程消费者，再重跑，并与未中断运行比对**库的全部内容**（`runs`/`processed_chunks`/`feature_windows`/`alarms`/`alarm_states` 五张表），不只比总行数。

用例本身做了三件事保证它到 Linux 上真的能证伪：①子进程起一个**监视线程**轮询消费者自己写出的 `processed_chunks`，看到第 1 行即写握手文件，父进程轮询到握手才发信号，不用固定 `sleep`，也不往业务代码里加测试专用参数；②断言 `returncode == -SIGKILL`，子进程若自己跑完（返回 0）用例即失败，不会静默通过；③重跑后逐表比对内容，写对行数但写错内容的重放也会被抓到。

**握手时机的真实边界**（按 A 第三轮评审 `5648720609` 的限定）：监视线程轮询 `processed_chunks`，看到第 1 行即写握手；但轮询**不能精确保证**此时"第一片刚提交、正处理第二片"，消费者可能已提交更晚的片。因此实际断言的是**某个非空的部分前缀**（`1 <= 已提交片数 < manifest.parts` 总数），不是"恰好一片"。本报告不把该用例描述成能证明更精确的时序。

**Linux 留证（C6，2026-11-02，Issue #51）**：C6 在被测树 main `7daa07c` 上分别于 Ubuntu 24.04.4 与 RHEL 9.7（均 Python 3.12.16）执行 `python -m pytest tests/fault_injection/test_pipeline_faults.py -q`，两次都是 **9 passed，无 skip**（1.12 s / 0.81 s）。该文件在本分支与 main 上逐字节相同（blob `19cbcab`），所以这条留证直接对应本节用例，不是另一份实现的结果。

**在 Windows 上仍然 skip**（`kill -9` 是 POSIX 语义，Windows 无等价物）：本机实测 `8 passed, 1 skipped`，skip 的正是这一条。Windows 上**不伪造**该项结果，也不把 Linux 的通过数抄进 Windows 那几行。

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

### 5.1.1 当前 main `195f5aa` 门禁复跑（2026-11-04）

本次只在 `195f5aa` 上补数字，不改任何口径：被测链路、配置、规则与 §2 相同（`configs/health_rules.yaml`、`configs/consumer.yaml`、`configs/benchmark_1m.yaml` 三份文件的 CRLF 工作树摘要与 LF blob 摘要，在 `2054bb5..195f5aa` 之间没有任何变化，已用 `sha256sum` 逐份复核）。环境 Windows 11 / Python 3.12.15 / NumPy 2.5.4，干净 worktree。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `pytest -q tests/integration tests/fault_injection`（Issue 验收） | 0 | **155 passed, 6 skipped in 20.11s** |
| `pytest -q` | 0 | **755 passed, 14 skipped in 25.72s** |
| `G15_RUN_STRESS=1 pytest -q` | 0 | **759 passed, 10 skipped in 28.29s** |
| `G15_RUN_STRESS=1 pytest -q -s tests/integration/test_stress_repeatability.py` | 0 | **4 passed in 3.24s** |
| `pytest -q tests/performance` | 0 | **6 skipped**（opt-in，需显式开启） |
| `pytest --collect-only -q` | 0 | **769 collected**（分目录见 §6） |
| `black --check src tests` | 0 | **53 files would be left unchanged** |
| `pylint src/g15 --fail-under=8.5` | 0 | **9.91/10** |

14 条 skip 的构成与 §5 一致：6 条性能 opt-in + 4 条压力用例 + 1 条 Linux `kill -9` + 3 条符号链接权限用例。§5 与 §5.1 那两张表分别是历史基线 `75b11945` 与主线 `8eee3ca` 的输出，保留原样，不和本表混用。

### 5.2 压力测试实测（重复执行、并发与中断恢复，2026-10-31）

口径见 `docs/test_plan.md` §5.2。用例已入库为 `tests/integration/test_stress_repeatability.py`（4 个测试函数，`G15_RUN_STRESS=1` 才跑），不再是脚本。

**A. 自动化用例实跑（2026-11-02 重建到 main `4b7266dd` 后复跑，2026-11-02 晚在 main `2054bb5` 上再复跑一次，Windows 11 / Python 3.12.15 / NumPy 2.5.4）**

```bash
G15_RUN_STRESS=1 python -m pytest -q -s tests/integration/test_stress_repeatability.py
```

重建分支上连跑 3 轮，每轮 **4 passed**（2.86 / 5.03 / 4.31 s），无偶发失败。下面是首轮落库时记的逐用例结果：

| 用例 | 实测 |
|---|---|
| `test_repeated_production_lands_byte_identical_shards` | 3 次生产的 `(part_id, rows, sha256)` 全等；3 次耗时 0.091 / 0.084 / 0.085 s（另几轮 0.092/0.093/0.080、0.095/0.083/0.086；有一轮首项 0.743 s，是冷启动抖动，只作记录不作判据） |
| `test_repeated_consumption_adds_no_row` | 首轮写入 256 窗口 / 1 告警；后两轮 `windows_written=0`、`alarms_written=0`、`shards_processed=0`、`shards_skipped=7`，五表行数不变 |
| `test_racing_consumers_never_duplicate_rows` | 3 个 spawn 消费者：典型结果是一个 `written/256` + 两个失败（`IntegrityError` 唯一键冲突或 `DatabaseError` 锁），也出现过两个成功（先跑完的写 256，后进来的写 0）；四轮最终库内容都等于单进程跑一遍的结果，无重复行 |
| `test_abruptly_stopped_consumption_replays_to_the_same_content` | `benchmark_1m` 10 分片，第 1 片事务提交后 kill，停在 `processed_chunks=1/10`；重放写 3506 窗口 / 9 告警、处理 9 片跳过 1 片，最终 `runs 1 / chunks 10 / windows 3906 / alarms 9 / alarm_states 1`，五表全量行与未中断对照库逐行相同 |

**B. 默认门禁不受影响**：不设 `G15_RUN_STRESS` 时这 4 条全部 skip。main `195f5aa` 上实测 `pytest -q` = **755 passed, 14 skipped in 25.72s**（14 条 skip = 6 性能 opt-in + 4 压力用例 + 1 Linux `kill -9` + 3 符号链接）；设 `G15_RUN_STRESS=1` 后全量是 **759 passed, 10 skipped in 28.29s**；同一 SHA 上单跑该文件复跑一轮 4 passed in 3.24 s（`2054bb5` 那轮是 2.86 / 5.03 / 4.31 s）。

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

### 5.3 生产基准百万点实测（Issue #51，2026-11-02）

口径见 `docs/test_plan.md` §5.3。被测 SHA **main `7daa07c410edd0a99c1c94b1794edbf2046cb705`**（含 #92 的 `benchmark` 子命令），配置 `configs/benchmark_1m.yaml`（`1,000,000` 点、`seed=42`、`chunk_points=102400`、无注入事件），每轮一个全新进程 + 独立空运行目录。

#### A. 双 Linux 正式留证（C6 交付，验收口径）

| 系统 | 轮次 | 生产窗口 `perf_counter` (s) | 独立校验 (s) | CLI 总墙钟 `/usr/bin/time` (s) | 峰值 RSS 整进程 (KB) | 退出码 |
|---|---|---|---|---|---|---|
| Ubuntu 24.04.4 | 1 | 0.1450 | 0.0585 | 0.31 | 50928 | 0 |
| Ubuntu 24.04.4 | 2 | 0.1385 | 0.0489 | 0.29 | 50580 | 0 |
| Ubuntu 24.04.4 | 3 | 0.1831 | 0.0482 | 0.34 | 50404 | 0 |
| RHEL 9.7 | 1 | 0.1381 | 0.0564 | 0.49 | 49924 | 0 |
| RHEL 9.7 | 2 | 0.1264 | 0.0531 | 0.28 | 49700 | 0 |
| RHEL 9.7 | 3 | 0.1299 | 0.0502 | 0.28 | 49832 | 0 |

解释器 pp03 / Python 3.12.16（NumPy 2.5.4、PyYAML 6.0.3，与主锁一致）；进程外工具 GNU time 1.9。6 轮全部 `status=complete`、`verification=passed`、10 分片 / `1,000,000` 点 / `2,000,000` 标量值，退出码 0。最慢一轮的生产窗口 **0.1831 s**，距 60 s 门槛有两个数量级余量。

- 峰值 RSS 是**整个被测进程的历史最高水位**，含解释器、配置加载与同进程内执行的校验开销；**不是**生产计时窗口专属峰值。
- `ru_maxrss` 前后相减当单次峰值的做法已被 A 否决，本表未使用。
- `cli_wallclock_seconds=unmeasured` 是子命令按 A 裁定故意不内置；表中"CLI 总墙钟"即 `/usr/bin/time -v` 的 Elapsed，覆盖解释器启动与全部子进程。
- 原始输出：两台 VM `~/g15-evidence/issue51-ubuntu2404-7daa07c-run{1,2,3}.{stdout,stderr,time,config}.txt` 与 `issue51-rhel97-7daa07c-run{1,2,3}.{stdout,stderr,time,config}.txt`。

#### B. Windows 预检（不代填验收）

同一 SHA、同一配置，Windows 11 / Python 3.12.15 / NumPy 2.5.4，三轮独立空目录，进程外用 `perf_counter` 包住整个子进程（含解释器启动）：

| 轮次 | 生产窗口 `perf_counter` (s) | 独立校验 (s) | CLI 总墙钟 (s) | 分片 | 点数 | `status` | `verification` | 退出码 |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.1617 | 0.0750 | 0.42 | 10 | 1000000 | complete | true | 0 |
| 2 | 0.1731 | 0.0860 | 0.45 | 10 | 1000000 | complete | true | 0 |
| 3 | 0.1560 | 0.0731 | 0.42 | 10 | 1000000 | complete | true | 0 |

Windows 上没有 `/usr/bin/time -v` 等价物，**峰值 RSS 未测**，如实记为未测，不用估算值或 Linux 数字代填。本表只作预检，验收以 A 表为准。

## 6 用例规模汇总（`docs/acceptance.md` §4 的 ≥15 单测 / ≥20 用例）

| 目录 | 基线 `75b11945` | 主线 `8eee3ca` | `2054bb5` | 当前 main `195f5aa` |
|---|---|---|---|---|
| `tests/unit/` | 521 | **543** | **598** | **601** |
| `tests/integration/` | 0 | **118** | **152**（含 `test_stress_repeatability.py` 4 条，默认 skip） | **152**（同上） |
| `tests/fault_injection/` | 0 | **9** | **9** | **9** |
| `tests/performance/` | 6 | **6** | **6** | **6** |
| `tests/test_package_import.py` | 1 | **1** | **1** | **1** |
| **合计** | **528** | **677** | **766** | **769** |

基线 `75b11945` 上 `tests/integration/` 与 `tests/fault_injection/` 只有 `.gitkeep`，各 0 个测试文件，实测 `pytest --collect-only -q` 为 `528 tests collected`。基线列按此更正（C4 在 #84 指出原 53/9/590 有误，A 于 `5b1e315e` 一并修正）。

单元测试远超 15 条、可执行用例远超 20 条；本报告只统计**实际跑过**的（见 §5、§5.1、§5.2）。各列都是 `pytest --collect-only -q` 的收集数，不是"通过数"。当前 main `195f5aa` 上默认跑过的是 755 条（769 收集数减 14 条 skip）；4 条压力用例要显式开 `G15_RUN_STRESS=1` 才计入，开了是 759 条。`unit` 从 598 增到 601 来自 #103（防漂移断言）与 #104（burst 短时长用例改写）；`integration`/`fault_injection`/`performance` 三条路径在 `2054bb5..195f5aa` 之间没有改动。

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

1. **双 Linux 实测（G4 已补、最终发布待复证）**：`kill -9` 见 §4.3，百万点见 §5.3；公开仓库全新克隆见 #10 评论 `6582119780`。主链接通后的 `545a598c` 又在 RHEL 9.7 / Ubuntu 24.04.4 完成 setup→clean→demo→verify→benchmark，均退出 0，并各跑 abrupt-stop 1 passed、故障注入 9 passed（#10 评论 `6759021839`）。仍未完成的是 #56/#9 独立保留集检出率的双系统复现，以及 `main@997acaeb` 之后最终候选 SHA 的整包复证；本报告 §3 的开发期检出率不能替代。
2. **`kill -9` 恢复**：Linux 侧已有双系统留证（§4.3，Ubuntu 24.04.4 / RHEL 9.7 各 9 passed 无 skip）。本机 Windows 无 POSIX 信号语义，仍按平台 skip，不在 Windows 上伪造该项。
3. **百万点 60 秒的结论**：生产基准计时方法见 `docs/test_plan.md` §5.3、实测数字见 §5.3（含 C6 的双 Linux 正式留证）。本报告 §3 的受控轮廓只有 65536 点，两套口径不能互换，也不能拿受控轮廓的成绩代替生产基准。
4. **真实工业准确率**：本报告的"100%"只指 §3 预定义教学测试集上的事件召回率。
5. **去抖/升级/恢复（N>1）**：受控轮廓固定 N=1，未覆盖 N>1 边界。
6. **规则内容指纹**：`scoring_version` 变更会被拒收，但同版本改规则内容或改尾窗策略目前不会自动检查（#57 遗留 P2，C2/A 侧收口）。
7. **`verify_run` 不校验时间轴**：七项 checks 不含 `timestamp_ns` 的绝对时间公式（`start + seq / sample_rate`）。`timestamp_ns` 整列置 0 或倒序、摘要重算后仍 `passed=True`，已实测（§5.1）。原始分片完整性通过不等于时间轴核验，不得在答辩中说"产物全部核验通过"。
8. **压力测试的边界**：§5.2 已覆盖重复生产、重放幂等、3 进程争用同一 SQLite、以及本平台上的中途终止后重放；Linux `kill -9` 的正式留证在 §4.3（C6 双系统 9 passed），它属于故障注入那一类，不是压力测试的自动化。仍**没有**长时间浸泡（soak）、没有并发度梯度（只固定 3 个），也**不**把并发消费写成受支持能力——3 个消费者里通常只有 1 个成功，其余按唯一键冲突或锁超时被拒。不得拿这一节当"并发能力已验证"的证据。

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

## 9 期末 100 分标准增量状态（Issue #142）

本节只登记截至 `main@997acaeb` 可核对的事实与缺口；Issue #142 的文档修改没有新增运行证据，也没有在本轮执行测试。

| 评分/红线 | 已有证据 | 尚缺证据 | 本轮状态 |
|---|---|---|---|
| 功能与硬指标 | 主链、双 Linux 百万点、坏数据与 kill 恢复 | #56→#9 独立保留集；最终 SHA 全量现场包 | 待执行，不改既有成绩 |
| 可靠性 | SHA 拒收、部分提交重放、五表逐行对账 | 最终 SHA 断网运行、教师现场随机注入、soak | 待执行 |
| 工程规范 | 测试数量、配置外置、CI、Pylint≥8.5 | 新国标追踪和最终发布复证 | 文档对齐中 |
| 技术深度 | ADR、百万点、并发/竞态探针 | #135 已实现（#149）；#136 p50/p95/p99 与 EXPLAIN | 查询实现已交付；容量待执行 |
| 数据安全 | 运行期离线、真值隔离、日志禁敏感信息 | 最终 SHA 自动日志扫描、外发授权记录 | 待执行 |
| 人工确认 | 系统无执行器；`reset-alarms --yes --reason` 有实现/测试 | 真实设备接入不在本项目范围；未来需变更单/旁路/双人复核 | no-actuation 边界，不冒充设备验证 |
| append-only 审计 | reset 历史随状态保留 | 独立 INSERT-only 审计及 UPDATE/DELETE 拒绝 | **已满足（应用级只增，#143/#147）**；文件级防篡改未声称 |

完整新增用例及判据见 `docs/test_plan.md` §10。实际执行后应追加日期、精确 SHA、环境、命令、退出码、原始证据路径与失败记录；不得直接把表中“已有历史证据”改写成最终候选通过。
## 10 用户查询容量门禁（Issue #136，Gate 0 实测）

> 计划见 `docs/test_plan.md` §11；本节只写真实跑过的结果，属 #136，与 #9 受控事件检出是不同主题。

### 10.1 结论摘要

本小节至 §10.3 保留 #153（实现 head `c6ff2cbc`）的历史 Windows 数字，不是本次最终提交复测。时长是单次观察，复跑需另附源码 SHA、主机与命令，不将不同源码树的观察合并为稳定性能范围或门槛。

| 项 | 结果 |
|---|---|
| 集成测试（oracle + 可信计数） | **3 passed，退出码 0**（`tests/integration/test_query_capacity_accounting.py`，1.86s） |
| 性能低负载（opt-in） | **1 passed**（`G15_RUN_QUERY_LOAD=1`，1.59s；默认 1 skipped） |
| D0 响应正确性 | 历史工具报告全部 200、处理后窗口全覆盖和告警 1 条；告警重发由客户端丢弃，原始条目当时未全部进入 oracle，不能解读成服务端原始响应无重复或原始 0 违例 |
| 计数守恒 | `client_in_flight==0`、`server_in_flight==0`、`server_received==client_total`、`response_completed==received`、request_id 集合一致，0 违例 |
| 在途峰值 | client 10 / server 10 |

### 10.2 环境

Windows 11（AMD64）、Python 3.12.15、NumPy 2.5.4、`os.cpu_count()=20`。

### 10.3 低负载量测数字

| 项 | 值 |
|---|---|
| 串行 10 请求（page_size=10） | 0.11 s |
| 并发 10 在途（ThreadPoolExecutor） | 0.02 s |
| 翻页（windows 逐页 10 页、alarms 首页即耗尽） | 0.15 s |
| 客户端延迟（n=30） | min 0.000 s / p50 0.015 s / p95 0.032 s / max 0.032 s |

### 10.4 明确未覆盖

- D1（1,000 设备 / 3,906,000 行）与 D2（10,000 设备 / 39,060,000 行）未建、未跑；
- 高负载（100→500→1,000→…→100k）未授权未执行；
- 双 Linux 低负载兼容留证归 C6，本节未做；
- `server_queued / server_executing` 两层传输事件产品侧未发，见 `docs/test_plan.md` §11.3。

### 10.5 A 接管后的固定源码复测（2026-11-12）

实现固定提交 `5b59b89f0c9d20c48926bfcfa4ac2ee07dd08e4a`，两次 CLI 测量时工作树均 clean。环境为 Windows 11/AMD64、pp03 Python 3.12.15、SQLite 3.53.4、16 逻辑 CPU；这是本次机器观察，与 §10.2 的历史主机分别记录。随后只追加本节与证据摘要，不改变该实现。

| 检查 | 命令（pytest 的 basetemp/TMP/TEMP 均在笔记库外） | 结果 |
|---|---|---|
| 全量回归 | `python -m pytest -q` | 961 passed、15 skipped，87.82s，exit 0 |
| 容量定向及默认 opt-in | `python -m pytest -q tests/integration/test_query_capacity_accounting.py tests/performance/query/test_query_load.py` | 29 passed、1 skipped，4.04s，exit 0 |
| opt-in | `G15_RUN_QUERY_LOAD=1 python -m pytest -q -s tests/performance/query/test_query_load.py` | 1 passed，1.88s，exit 0；这只验证工具与本次10波次，不代表完整 Gate 0 |
| 静态 | `python -m black --check src tests tools`；`python -m pylint src/g15 --fail-under=8.5`；`python tools/check_doc_references.py --root .` | 80 文件 unchanged；9.89；0 issue；均 exit 0 |
| 变异检查 | 仅内存删除 windows count/set、alarms count/set 四个 guard，分别运行对应负例 | 四轮各 1 failed、exit 1（预期）；实现文件未改，证明断言有回归保护 |

命令为 `python -m tools.query_load.driver --concurrent 10 --output <新的10波次证据目录>`，第二轮仅将并发改50且使用另一新目录；默认 serial=10、workers=64。每轮只有一次固定提交观察，未为达标延迟业务或反复挑最好值。

| 波次 | 原始核验/消费覆盖/三方对账 | 客户端峰值/对应用户数 | 服务端峰值/对应用户数 | 延迟 p50/p95/p99（秒，包含串行和翻页） | 目标/退出码 |
|---|---|---|---|---|---|
| 10 | 全部无问题 | 10/10 | 10/10 | n=30，0.010094 / 0.039068 / 0.039071 | 达到10，exit 0 |
| 50 | 全部无问题 | 50/50 | **23/23** | n=70，0.108741 / 0.581241 / 0.584952 | **未达到服务端50，exit 1** |

两轮原始窗口/消费/丢弃均为100/100/0；告警为10/1/9。消费后恰好1告警不代表服务端原始无重复；raw coverage observations 保留原始10条告警与预期1条的差异。每轮70条 EXPLAIN 是10库×7类实际 SELECT helper 探针，包含告警续页分支，不是整个HTTP请求成本。资源仅是同进程前后快照，不能拆成独立两端或负载峰值。计时用 Windows QPC 单调高精度时钟，避免15.625ms的 GetTickCount64 量化误差。

可公开摘要、命令、源码、机器、实际峰值/延迟、资源限制及原始报告 SHA-256 见 [本次证据摘要](../tests/fixtures/query/evidence_136_20261112.json)。完整原始 JSON 和失败轮次由 A 留档；摘要不冒充原始证据。开发期一次日志阶段数量误判、一组测试fixture混用时钟的失败及其修复记录亦保留，不计为最终通过记录。

**完整 Gate 0 仍未通过**：50波次未达到服务端实际峰值；两 Linux 未执行（现有VM均未运行、连接方式未确认）；queued/executing阶段与独立两端资源仍缺。等待 B1/C4 对最终 head 真实复核；本 PR 不升档，不修改 #56/#9 的 seed、数据或冻结状态。

### 10.6 后续自审发现的漏检与修复复证（2026-11-12）

§10.5 保留其原始源码与单次结果，不回填成本轮成绩。A 在后续审查中复现三个问题：普通响应缺失/重复替换及未知拒绝或错误服务端终态可能假通过；耗尽流中的新增空页/页内重复可能被消费丢弃掩盖；存储或诊断异常可能使已有请求证据未写出。修复后逐条绑定原始响应与客户端字节记录、核服务端终态序列，并按请求是否省略 cursor 核每条流的原始首屏；普通失败按阶段留存，诊断失败保留原始记录。

实现固定提交为 `8c19769916c57568403bc51ac3c0badd41c9a9bb`。其运行代码与容量测试通过全量回归；本节和新摘要仅追加证据，不修改该实现。环境：Windows 11/AMD64、pp03 Python 3.12.15、SQLite 3.53.4、16逻辑CPU；测试、临时目录与原始报告均在笔记库外。

| 检查 | 命令 | 实际结果 |
|---|---|---|
| 全量 | `python -m pytest -q`（库外 basetemp/TMP/TEMP） | **977 passed、15 skipped**，86.23s，exit0；其中容量集成45项 |
| opt-in | `G15_RUN_QUERY_LOAD=1 python -m pytest -q -s tests/performance/query/test_query_load.py` | **1 passed**，1.47s，exit0 |
| 静态 | `python -m black --check src tests tools`；`python -m pylint src/g15 --fail-under=8.5`；`python tools/check_doc_references.py --root .` | 80文件unchanged；9.89；0 issue，均exit0；diff检查通过 |
| 可证伪回归 | 真实HTTP回包注入耗尽告警空页/重复、活动窗口重复/乱序；响应漏失/替换/改写；错误服务端事件；早晚存储异常及诊断异常 | 新增16项进入上述全量；错误响应必须判失败，异常后已有原始记录仍保存。分页4项先失败、修后通过；其余先失败记录与修后原始报告由A留档 |

固定 clean 源码仅各测一次：`python -m tools.query_load.driver --concurrent 10 --output <新目录>`；第二轮并发改50并用另一新目录，均默认serial=10、workers=64。未延迟业务、未重试择优。

| 波次 | 原始核验/覆盖/关联/日志对账 | client请求/用户峰值 | server请求/用户峰值 | p50 / p95 / p99（秒） | 目标与退出 |
|---|---|---|---|---|---|
| 10 | 全部无问题 | 10/10 | 10/10 | n=30，0.006925 / 0.024233 / 0.024995 | 达标，exit0 |
| 50 | 全部无问题 | 50/50 | **25/25** | n=70，0.066239 / 0.546116 / 0.548787 | **服务端未达50，exit1** |

两轮 windows raw/consumed/discarded=100/100/0，alarms=10/1/9；各70条EXPLAIN仍仅为实际SELECT helper探针，资源仍为同进程快照。早期存储失败可保存0请求的失败报告；后期存储或诊断失败可保存21条客户端、42条服务端事件与10页原始响应，并退出1。异常文本不入报告，仅记录安全类型；进程中断不会伪装为通过。

[本轮独立摘要](../tests/fixtures/query/evidence_136_20261112_t175.json) 含源码/文件摘要、机器、命令、原始报告大小与SHA-256。10波次原始报告SHA-256为 `d306a10f3522178a94d5eb6e16dd50333fddd58583dfa7ee9cad7f802bfcc66c`；50波次为 `f734b38cff2be29712f74910a42b8b153959fcd249953a0681ddd40ca880752a`。开发期临时探测中未保存下来的输出仅作为诊断摘要；不冒称原始JSON留档，复证以保留的反例、回归和固定源码报告为准。

**完整Gate0仍未通过**：服务端50目标、双Linux、产品阶段及独立两端资源仍缺。A已完成修复后的自审，未发现本增量剩余阻断；自审不计非作者批准。本修复候选等待本次合并决策，不升档、不触及#56/#9数据或冻结。

### 10.7 契约漏检修复与 Windows 分进程10/50（2026-11-12）

本节为后续增量，保留§10.5/§10.6全部历史失败及来源。实现与正式量测固定 `836948c77be0076e7beb2f0f2079d7bbd4fc0d9c`，两轮均clean；后续报告提交不修改实现。Windows11/AMD64、pp03 Python3.12.15、SQLite3.53.4、16逻辑CPU；测试/临时/原始报告全部在笔记库外。

已补query契约/信封/聚合版本、响应嵌套允许字段、严格JSON类型及sort/page_size独立校验；真实HTTP注入覆盖版本错/缺、审计字段泄漏、bool/float替代整数及nullable键缺失，measurement/CLI/opt-in均须失败留证。另补应用授权与传输终态的actor/权限域三方核对。

服务端新增配对执行事件和原发纳秒时钟/PID；Expect、非GET、过载、异常及写失败各留真实阶段。分进程工具核资源样本与汇总、真实服务端PID的一致性，持续流传原始事件和资源，晚期诊断失败仍将已有证据写盘。峰值保留对应request_id/用户集合，正式目标只按并发波次实际ID计算，不混入串行/分页区间。

| 检查 | 实际结果 |
|---|---|
| 本地全量 | 1094 passed、15 skipped，171.48s，exit0；末次诊断守护补丁前的完整套件 |
| 末次守护及分进程回归 | 20 passed，11.22s，exit0；含真实请求后的ArithmeticError写盘留存负例 |
| 峰值真实ID回溯 | 12 passed，0.05s，exit0 |
| opt-in | 1 passed，1.47s，exit0；复用原入口，不冒充正式容量波次 |
| 静态 | Black86文件、Pylint src9.88、docrefs0、diff检查通过 |

正式命令：`python -m tools.query_load.process_runner --serial 10 --concurrent 10 --workers 64 --output <新目录>`；第二轮仅concurrent改50并用另一新目录。各只运行一次，预连接后仅客户端发送前barrier，page_size=100真实读取D0全100窗；服务器无barrier、sleep、人工延迟、限速或自动重试。与§10.6同进程/page10负载不同，不把两轮差值声称为同工作负载性能提升。

| 波次 | 并发client请求/用户峰值 | 并发server在途请求/用户峰值 | server执行请求/用户峰值 | 并发p50/p95/p99（秒） | 结果 |
|---|---|---|---|---|---|
| 10 | 10/10 | 10/10 | 10/10 | 0.033024/0.037651/0.037651，n=10 | quality=true、target=true、exit0 |
| 50 | 50/50 | 50/50 | **49/49** | 0.232502/0.271337/0.274439，n=50 | quality=true、target=true、exit0 |

并发延迟从实际发送前到完整读取并解码完成，不含预连接和barrier等待；串行/分页含建连，分相分位数另留原始报告。50的接收至交付在途达标，不冒称执行阶段达到50。两轮客户端记录30/70，传输事件120/280，应用事件180/420；各项原始oracle/覆盖/逐响应关联/身份域/阶段/资源/固定源码对账均无问题。分页windows原始/消费/丢弃100/100/0，alarms10/1/9。

两端为不同进程，分别保存CPU区间差值、RSS/handle采样、I/O计数、主机内存和卷容量；10轮client/server RSS采样最大49,934,336/45,047,808 bytes，50轮57,102,336/52,539,392 bytes。采样最大只是已观察下界，CPU短区间受OS计时精度影响。进程网络wire bytes仍未测，HTTP正文接收字节另列，不能把进程I/O或主机流量当作两端网络。

[本轮摘要与哈希](../tests/fixtures/query/evidence_136_20261112_t177.json)记录全部检查、阶段峰值/用户集合、分相延迟、资源及原始报告大小和SHA-256。10报告为 `b4f1e583e791dc40c11b531ff3dc002309d8b2f693e5024a7cbc38a59c046a67`；50报告为 `a7722d16a4c3e36d5fcda323941ccfe9c0f91ae05e493ecde24160d5300b1316`。

完整Gate0保持未通过：同SHA双Linux尚待SSH接入后执行，queued因无应用队列而保持null，其N/A裁定、非作者契约/计数复核及阶段验收待补；双流耗尽重发仍保留原始证据边界。CI按最终PR head实时结果单列，A自审不计独立批准；本轮不升档、不改#56/#9数据或冻结。

### 10.8 同实现三平台 D0 一分钟观察（2026-11-12）

本节是§10.5–§10.7之后的完整观察增量，旧失败和单波数字原样保留。全部六档正式观察固定clean源码 `2bcd639668c679c2b5f01ad10e6888fe454d3f00`，前后内容不变；每档只执行一次，三平台依次执行，没有并行压测或失败后择优重跑。其后`487f9915`新增minute证据产物并补报告，`0632e32f`/`0b043093`更正文档；`4481f1bb`则修改query_load客户端的代理隔离与对应测试，并非只有文档差异。上述修订未重采或回写原六档，六档来源仍为clean`2bcd639`，不将后续修订提交号冒充实际测量SHA。

已修复的oracle版本/字段投影/严格类型与身份域漏检、真实执行阶段及分进程资源沿用§10.7；本次另补socket应用流计数、逐连接反向四元组对账和有界的一分钟观察。D0是10设备×100窗，真实认证授权/HTTP/SQLite；先一用户10请求和真实page10完整分页，再以page100重复发起10/50个不同授权用户波次，同一档持续使用一个真实服务端进程。公开数据投影（10设备、1000窗、10告警、10状态）六档SHA-256一致：`990707d9fe56da2d66f2523813e71d630f3aad31e8c16ae6293d46d713ea9cfc`；此值是独立SQL核过的公开响应投影摘要，不是物理或private数据库字节摘要。

正式命令：`python -m tools.query_load.observation_runner --concurrent <10或50> --seconds 60 --workers 64 --output <新的库外目录>`。CLI拒绝短于60秒；客户端在预连接后的实际发送前同步，服务端无barrier、sleep、压住响应或限速。每个波次实际发送至完整读取并解码的跨度累计至少60秒；准备连接、同步等待、验证、落盘与波间空档不计入有效时长。墙钟与空档单列，**这没有证明连续60秒恒定50在途或稳态吞吐**。

| 平台/授权用户 | 完成波数/达标波数 | client/server请求峰值范围 | 有效/墙钟/空档（秒） | 全并发p50/p95/p99（秒） | 质量/当前候选目标/退出码 |
|---|---|---|---|---|---|
| Windows/10 | 1324/1324 | 10–10/10–10 | 60.011511/161.714666/101.703155 | 0.037260/0.052913/0.070781，n=13240 | true/true/0 |
| Windows/50 | 210/209 | 50–50/49–50 | 60.091891/141.601609/81.509718 | 0.220876/0.368540/0.449114，n=10500 | true/false/1 |
| Ubuntu/10 | 407/407 | 10–10/10–10 | 60.141299/89.131081/28.989782 | 0.131356/0.165727/0.183909，n=4070 | true/true/0 |
| Ubuntu/50 | 50/50 | 50–50/50–50 | 60.272873/76.483366/16.210492 | 0.983168/1.278572/1.367290，n=2500 | true/true/0 |
| RHEL/10 | 862/814 | 10–10/8–10 | 60.011470/129.977514/69.966044 | 0.055863/0.079157/0.093529，n=8620 | true/false/1 |
| RHEL/50 | 116/98 | 50–50/47–50 | 60.412616/100.046165/39.633550 | 0.421962/0.629113/0.680772，n=5800 | true/false/1 |

请求与已验证不同用户的峰值逐波分别保留，不能以累计人数、连接数或server执行峰值替代接收至完成在途。p50/p95/p99由每档全部主并发请求的原始延迟按nearest-rank重算，包含未达波，排除串行/分页前置；不是逐波p99的平均值。全部六档quality=true、HTTP错误0，源码前后clean/unchanged；全部原始档案大小/SHA和socket对账检查通过。

Windows50唯一未达波为`wave-00192`：client50、server49，50个200响应、50个唯一ID与三方日志完全对账。原发服务端纳秒事件独立重算仍为49；首个完成比最后received早23.4952ms，属于实际重叠不足，没有丢请求或查询错误。其余209波server50。当前§11.8的保守候选把各波目标作AND，因此本档必须保留target=false/exit1；209/210是波次达峰比例，不是HTTP成功率或SLA。统计/发起窗口/所选在途阶段尚待真实Review；不得见到结果后改阈值或用最好波覆盖。

RHEL10有48个未达波；client请求峰值范围10–10，server实际峰值范围8–10，按同一AND候选保留target=false/exit1。完整wave_id/峰值清单见摘要；本批没有改同步策略、伪造在途或择优重测。

RHEL50有18个未达波；client请求峰值范围50–50，server实际峰值范围47–50，按同一AND候选保留target=false/exit1。完整wave_id/峰值清单见摘要；本批没有改同步策略、伪造在途或择优重测。

短请求之间未完全重叠不能直接解释为服务端容量上限；RHEL10的质量与资源证据正常后，继续的是同批已授权50档独立观察，没有把10档改判通过或升入更高负载。

每档原始NDJSON只保留一份base64响应，索引保留逐波hash/峰值用户和request_id/实际区间；内存只保留当前波，1GiB/10000波保护未触发。下表资源为包含前置在内的各观测区间最大记录，RSS/FD/handle采样最大仅为已观察下界；CPU为单波进程CPU差值占该机逻辑CPU容量的区间比率最大，不是瞬时CPU或全主机饱和度。

| 平台/档 | client/server RSS最大（bytes） | client/server FD或handle最大 | client/server CPU容量比最大 | client发/收应用流（bytes） |
|---|---|---|---|---|
| Windows/10 | 190058496/166494208 | 717/359 open_handles | 6.6669%/11.4861% | 5804759/252685878 |
| Windows/50 | 152825856/113930240 | 760/735 open_handles | 2.5634%/7.8578% | 4599689/200439558 |
| Ubuntu/10 | 65511424/58912768 | 21/51 open_fds | 3.4268%/10.7622% | 1792884/77832318 |
| Ubuntu/50 | 88465408/78405632 | 61/163 open_fds | 3.6237%/15.2972% | 1107009/47895558 |
| RHEL/10 | 59617280/50577408 | 21/51 open_fds | 22.1928%/35.5731% | 3783509/164591718 |
| RHEL/50 | 79302656/70836224 | 61/152 open_fds | 12.9467%/43.2637% | 2552079/110819958 |

client/server是同机不同进程，经127.0.0.1真实TCP/HTTP；不是跨主机或VM/NAT业务流量，SSH只作调度/取证。socket证据计入HTTP头和正文，真实反向四元组的发送/接收字节逐连接一致，分别绑定不同client/server PID；未把进程文件I/O或主机流量当作网络。它不包含TCP/IP/链路头、重传或物理链路容量，不能证明NIC余量。完整原始CPU/RSS/FD/handle/I/O、主机内存/卷、事件、实际SELECT查询计划、断言及失败记录均留在库外；缓存未控制，不外推冷/热性能。

| 完整回归/环境 | 真实结果与来源 |
|---|---|
| Windows11/AMD64，16逻辑CPU，pp03 Python3.12.15/SQLite3.53.4 | 1131 passed/15 skipped，184.53s，exit0；此全量在提交前工作树执行（原log de0a9b21、dirty=true），60个采证源码hash逐一匹配2bcd639、0不一致；不能称在clean新提交重跑全套。全部正式分钟观察均在clean2bcd639执行。 |
| Ubuntu24.04.5/x86_64，16 vCPU，独立pp03 Python3.12.16/SQLite3.54.1 | 新克隆clean2bcd639，1132 passed/14 skipped，145.91s，exit0；Black90、Pylint9.88、docrefs0。 |
| RHEL9.7/x86_64，4 vCPU，独立pp03 Python3.12.16/SQLite3.54.1 | 新克隆clean2bcd639，1132 passed/14 skipped，143.94s，exit0；Black90、Pylint9.88、docrefs0。 |
| exact实现CI | [workflow706/run43149395625](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43149395625)，四job全部success；Python3.11/3.12各1132 passed/14 skipped，80.30/81.08s，Pylint9.88。 |

Linux先核官方Miniforge安装包SHA及锁文件的Git规范LF摘要，再仅在用户拥有的临时根建立隔离环境；无sudo、系统Python或shell启动文件修改。Windows/Linux skipped数量差别按平台测试条件保留，不合并成同一测试总数。三平台CPU/虚拟化及依赖版本存在差别，不将分位数差异解释为操作系统性能排名。文档摘要提交的CI另以最终PR checks为准。

[六档摘要与文件哈希](../tests/fixtures/query/evidence_136_20261112_t177_minute.json)包含命令、机器与运行时版本、精确源码、实际峰值范围/所有未达波、原始分位数、两端资源、socket对账及summary/index/source/query-plan SHA。大批原始档案由A在笔记库外留存，不把测试目录、数据库、缓存或执行脚本移进规划库。

**完整Gate0仍未通过**。本轮补齐三平台同实现的D0一分钟观察与失败证据。评审记录截至2026-11-12 12:59北京时间：B1于12:19批准实现2bcd639的旧票后被DISMISSED；随后已于12:40对0632e32另提交[APPROVED（5869226274）](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/157#pullrequestreview-5869226274)，明确核对新增六档摘要并同意不升档。C4于12:59对同一0632e32提交[CHANGES_REQUESTED（5869358104）](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/157#pullrequestreview-5869358104)，认可技术及摘要自洽，留下代理隔离、#136授权登记、陈旧批准状态三项P2。上述票只绑定各自提交；后续修订须重新核对远端状态，不能沿用已失效票或写成从未取得非作者批准。A的精确阶段裁定仍待登记。应用无队列，queued保持null，其N/A须显式裁定；耗尽并行流的原始告警重发仍明示（page10遍历原始/消费/丢弃10/1/9），不能把消费后去重当作服务端原始零重复；若要求原始零重复，需要明确的耗尽流协议修订。socket对D0资源是否充分、物理网络/受控缓存边界亦待复核。A内部复核不计独立批准；#157保持未合并、#136保持open，不升档至D1/D2/100k。

### 10.9 C4 三项 P2 修订与原始证据边界（2026-11-12）

- 代理：C4在Windows环境设置http_proxy后真实复现网络字节对账失败；清除代理环境后其回归1131 passed/15 skipped。原T177六档保留的socket反向端点与应用字节对账支持本机直接连接路径，但没有采代理环境变量快照，不能追溯宣称所有代理变量均为空。修订后的query_load客户端与urllib网络测试显式使用ProxyHandler({})，既不读取环境/系统代理，也不修改用户环境。修复当时的真实代理陷阱矩阵得到2 failed（GET/POST均实际进入代理并502）；C4在当前九项污染矩阵中将QueryClient退回全局urlopen后，独立复现3 failed（environment/system_discovery/installed_opener三个query_client用例，见[新head复核](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/157#pullrequestreview-5873323270)），两组红例属于不同测试矩阵，不能混作同一次结果。修复后网络与原客户端异常回归合计38 passed/1.81s；三类污染×三类入口的9项真实HTTP检查均代理0访问/代理发现0调用，保留原反向端点和双向字节严格断言。Black通过，三修订文件Pylint8.97。Windows注册表只核对标准库发现代码及返回配置模型，未读取或修改实际注册表。
- 全量来源：本次文档修订未重跑全量或容量；修订前候选`0b043093e61170dc4719efbbf37ee56067ee4033`的全量以[CI716](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43170955760)为准，A未在该候选的clean树重跑全量。C4在[新head复核](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/157#pullrequestreview-5873323270)中另报告该候选1138 passed/15 skipped、272.77s（Windows11/AMD64、16逻辑CPU、pp03 Python3.12.15，保留环境代理）；此为C4独立运行，与§10.8的T177实现/历史工作树结果分列，不能记作A对本次文档提交的新实测。
- 授权：A已在#136正文的精确授权节与文件白名单登记service/http.py的阶段观测窄范围，以及对应集成回归/接口同步；test_plan §11.1引用该登记。此不授权评分/存储/查询结果变化，不替代真实Review。
- 状态：§10.8分别记录已失效实现票、B1对0632e32的新批准及C4修改请求；旧分钟证据与失败不覆盖，新提交按远端实际票状态核验。
- 维护归属：CODEOWNERS加入A是#136接管后工具/证据维护的候选归属；B1/C4仍列为候选。候选owner不是已批准，作者A不自批，内部复核不重复计为独立批准。
- 本修订不重采T177容量、不调整AND门槛，不以代理修复或10台仿真设备实测替代完整Gate0。原六档源码仍为clean2bcd639，所有未达波和退出码保持原义。

### 10.10 #162 选流与连接准备修订后的三平台固定源码观察（2026-11-13）

**A按现行每波目标AND候选判定：完整Gate0仍未通过。** 六档均达到有效时长、质量通过且HTTP错误0；唯一未达是Ubuntu10的`wave-00024`，server真实峰值9，原exit1保持。它不能推断“服务端最多只能服务9用户”。§10.8的T177实现、六档数据、67个未达波及§10.9失败来源均原样保留，不能与本节合并样本或改判。

本次正式实现固定为`dba4dce9b326a883f572ce0809ce1d5f8275bd11`，Git tree为`c7bd8439caf6771d509026c1409f3bea9b55096c`；六档前后均clean/source unchanged。紧接该次量测的`ed884c991ef350ab11bc73b51d85d1b39da44bb0`仅追加本节和[公开白名单证据摘要](../tests/fixtures/query/evidence_162_20261113_gate0.json)；本轮selector为之后的独立实现与工作负载，须另绑新源码和观察证据，不改该次正式量测源码或原始结果。完整raw、逐波报告、index、控制记录及失败由A在笔记库外留档；摘要只给相对artifact标签与SHA-256，不包含机器路径、主机名、SSH地址、凭据、cursor值或完整响应。

同一SHA的双Linux正式D0证据现已补齐，不把原summary静态remaining_gates中的“two Linux evidence”当作采完六档后的最新缺口；原summary/aggregate不改。当前仍待处置的是唯一真实target miss、B1/C4对精确head协议/同步统计/负载端调度与资源/cache充分性的真实Review，以及A的阶段裁定。

`source.source_file_sha256`是整份source.json文档bytes的摘要，包含平台/机器元数据，三平台不同是正常的；每档前后content_sha256源码映射稳定、同一Git tree及跨Linux64项映射0差异另行核对。六档相同的是下述公开投影hash，不声称source.json文档hash跨平台相同。

相同D0为10设备×100窗，真实认证授权、HTTP和只读SQLite。每档先做10设备page100串行查询及一次真实page10完整遍历，再由10/50个不同授权用户重复page100波次；同档始终使用同一个真实服务端进程。命令为`python -m tools.query_load.observation_runner --concurrent <10或50> --seconds 60 --workers 64 --max-waves 10000 --byte-budget 1073741824 --output <新的库外目录>`，各档只执行一次、拒绝覆盖；顺序为Windows10→50、Ubuntu10→50、RHEL10→50，期间无并行测试、负载或大文件分析。

选流首请求显式both，之后仅查询未耗尽的流；六次page10均为windows raw/consumed/discarded=100/100/0、alarms=1/1/0。旧v1回放负例仍保留，原始全程重复/缺失、假omitted、版本降级或请求上下文缺失会使质量失败。10设备1000窗/10告警/10状态的公开page100投影六档SHA-256均为`990707d9fe56da2d66f2523813e71d630f3aad31e8c16ae6293d46d713ea9cfc`；此为去掉顶层request/trace ID、按run排序并规范JSON后的公开投影，不能当作物理或private数据库字节摘要。

准备阶段只确认本波精确连接端点与handler reader安装，控制ACK后才释放客户端发送前barrier；headers此前只在内存组装，正式计时从实际endheaders发送前至完整读取并解码。prepared不计received、认证用户或在途；服务端无barrier、sleep、压住响应、限速或队列扩容。有效时长只累计质量通过的真实主波跨度，准备/前置/核验/落盘/波间空档均不计入；这不证明连续60秒恒定N在途或稳态吞吐。

| 平台/授权用户 | 完成/达峰波 | 主请求数 | client / server在途范围 | executing范围 | 质量/AND目标/原退出码 |
|---|---|---|---|---|---|
| Windows/10 | 1227/1227 | 12270 | 10–10 / 10–10 | 10–10 | true/true/0 |
| Windows/50 | 233/233 | 11650 | 50–50 / 50–50 | 47–50 | true/true/0 |
| Ubuntu/10 | 317/316 | 3170 | 10–10 / 9–10 | 9–10 | true/false/1 |
| Ubuntu/50 | 48/48 | 2400 | 50–50 / 50–50 | 36–50 | true/true/0 |
| RHEL/10 | 867/867 | 8670 | 10–10 / 10–10 | 5–10 | true/true/0 |
| RHEL/50 | 100/100 | 5000 | 50–50 / 50–50 | 19–50 | true/true/0 |

在途与对应不同授权用户的峰值范围逐波重算并保留，不能用连接数、准备数或executing替换。上表server仅指候选`server_received→response_completed`；executing独立计数。六档合计**2792波、43160个主并发请求**，另有120个前置请求；2791/2792是达峰波数，不是HTTP成功率、SLA或新的接受阈值。

| 平台/档 | 有效/墙钟/空档（秒） | 全局主请求p50/p95/p99（秒） |
|---|---|---|
| Windows/10 | 60.050526 / 170.033900 / 109.983374 | 0.041105 / 0.054886 / 0.072431，n=12270 |
| Windows/50 | 60.012153 / 144.050703 / 84.038549 | 0.210483 / 0.292567 / 0.334384，n=11650 |
| Ubuntu/10 | 60.052344 / 83.127926 / 23.075583 | 0.173034 / 0.212236 / 0.225267，n=3170 |
| Ubuntu/50 | 60.196581 / 73.666140 / 13.469559 | 1.060739 / 1.257463 / 1.320293，n=2400 |
| RHEL/10 | 60.006674 / 124.260245 / 64.253571 | 0.056983 / 0.077838 / 0.085945，n=8670 |
| RHEL/50 | 60.604963 / 92.146983 / 31.542020 | 0.512976 / 0.657887 / 0.692803，n=5000 |

分位数来自每档全部主并发原始延迟，采用全局`(n−1)p`线性插值，排除前置、包含未达波，不平均逐波分位数。它与§10.8的nearest-rank口径不同，两组值各自保留。六份原summary/index/aggregate与三份完整归档的SHA-256、大小及相对标签见fixture；Linux每个平台的一个归档包含10和50两档，Windows归档也包含两档及原离线审计失败版本，不能把共享归档称作六个不同档案。

唯一未达波的10请求、40个原发服务端阶段及raw/report哈希独立核对完整。只在服务端自身时钟内，以首received为0：首请求3.695799ms已response_completed，其余9条received都在之后；下一条received为4.813024ms，晚首completion 1.117225ms，最后一条为14.353793ms，故真实server峰值9。只在客户端自身时钟内：barrier释放跨度5.627401ms、发送包装start跨度5.621229ms、首/次发送包装start相隔4.758562ms；全部client starts早于首client complete，client峰值10同样正确。未做跨进程相减。

10个reader-installed及recv_into包装start均早于服务端ACK，最近余量0.020293ms，不能据此把miss归为缺少reader准备。Python包装start不等于内核syscall entry；GIL/内核调度、实际到包等环节未观测，不能定因或推导容量上限。诊断JSON SHA-256为`eb091639f8f89441ea93e4214acaa80d3e18bc1ea7bae333a575e1f6ff0c38fd`，诊断Markdown为`0bc68247d6cb2c3d5b580a08fcb12c6695621e3ed36e5e8c2f96a2403f4cb320`；它们与原波raw/report各自哈希分列于fixture，不能混绑。当前记录未定位可据以继续修改的明确计数、准备或网络产品缺陷，A未盲目重构、加试刷绿或调整AND。

资源下表包含前置。RSS/FD/handle为已观察采样最大下界，不是精确区间峰；CPU是单波进程CPU差值/该波时长的区间比率最大，按原机逻辑CPU容量归一，非瞬时CPU或全主机饱和度。每核口径、nullable字段、样本数及完整端点应用字节在fixture分列。

| 平台/档 | client/server RSS采样最大（bytes） | client/server FD或handle | client/server CPU容量比最大 | client主波发送/接收HTTP应用字节 |
|---|---|---|---|---|
| Windows/10 | 189718528/166506496 | 693/360 open_handles | 7.3950%/12.0959% | 5368125/233964360 |
| Windows/50 | 154890240/116559872 | 754/671 open_handles | 2.3663%/8.5654% | 5089885/222142200 |
| Ubuntu/10 | 66723840/60682240 | 21/51 open_fds | 3.4095%/10.0250% | 1386875/60445560 |
| Ubuntu/50 | 89661440/79413248 | 61/171 open_fds | 3.3010%/11.1166% | 1050960/45763200 |
| RHEL/10 | 58646528/52289536 | 21/51 open_fds | 23.6495%/35.3307% | 3793125/165319560 |
| RHEL/50 | 80773120/72704000 | 61/164 open_fds | 11.9049%/42.5954% | 2189500/95340000 |

两端是同机不同进程的真实loopback HTTP；socket计HTTP头与正文，逐连接反向四元组及双向字节匹配。未把文件I/O、主机共享流量或SSH传输当作业务网络；不包含TCP/IP/链路头、重传或物理NIC容量。缓存未控制，不声称冷/热、稳态或资源整体充分。queued仍为null/application_queue=absent，listen_backlog未测；按interface §17.3/test_plan §11.9仅有条件N/A，须真实非作者复核及A明确裁定，不能推出排队0或该项通过。

| 完整回归/来源 | 实际结果 |
|---|---|
| Windows11/AMD64，16逻辑CPU，pp03 Python3.12.15/SQLite3.53.4 | exact clean dba4dce9：1247 passed/15 skipped，222.63s，exit0；Black95、Pylint9.87、docrefs0。 |
| Ubuntu24.04.5/x86_64，16 vCPU，独立pp03 Python3.12.16/SQLite3.54.1 | 全新clone exact dba4dce9、前后clean：1248 passed/14 skipped，166.39s，exit0；Black95、Pylint9.87、docrefs0；21锁包0差异、64源码hash前后0差异。 |
| RHEL9.7/x86_64，4 vCPU，独立pp03 Python3.12.16/SQLite3.54.1 | 全新clone exact dba4dce9、前后clean：1248 passed/14 skipped，153.42s，exit0；Black95、Pylint9.87、docrefs0；21锁包0差异、64源码hash前后0差异。 |
| [CI736/run43265771901](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43265771901) | 四job success；3.11/3.12各1248 passed/14 skipped，116.25/115.50s，Black95、Pylint9.87。checkout日志实际为`refs/pull/165/merge`的`c0594899f213d56ac71cfe7152ae3d759cd3827a`，将dba合入`a10cab2f56cdd109632135ee21645b5c3b9ac064`；此CI与上面本地exact dba检出分别归属。 |

Linux多通过的1项来自Windows按平台跳过的POSIX SIGKILL恢复用例；不把两套skip总数拼成同一次结果。三平台CPU/虚拟化、Python/SQLite版本存在差别，不能把分位数差异解释为操作系统排名。回归与缓存/临时目录全部库外，未改旧环境editable或在观测期间重跑测试。

失败来源继续保留：[CI731/run43261142715](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43261142715)关联候选`c6a2a06051ac72f67344f68b47ad30b6bc7ff450`，原job131332461859为1 failed/1138 passed/14 skipped、107.75s，consumer已return0而watcher未取得中间态kill点；该job实际checkout未另取得，不补造SHA。#157夹具修订已在本次父提交a10cab2f中。开发期旧editable导致streams HTTP400、请求context多余字段与严格schema不符、并行本地提交触发fixed_source失败均为非冻结检查，原失败库外留存，来源不清不编SHA。首版离线脚本误要求合法v1串行page100必须显式both，导致Windows10派生审核10项失败；原v1输出保留，修正后固定脚本`98aafe48b4233db4b7bc2140e9d94c7ca9571df24d12ee378aff441c82627ee3`对六档全部审核有效、0问题，未重测任何正式档。离线审核核hash/计数/时序/投影/资源字节，保存SQL oracle原结论，没有重新查询私有fixture数据库或生成新工作量。

**下一步保持真实Review门禁。** A按当前AND不通过，不升档、不改性能SLO；同步窗口、负载端调度、所选在途阶段及资源/cache证据充分性须先交B1/C4真实Review，再决定后续。任何代码或重测均须另记新来源并保留当前六档；内部复核不计独立批准。本节证据完成不等于实现PR已接收或完整Gate0已通过。

### 10.11 #162 selector客户端修订与新来源三平台复证（2026-11-13）

本节记录A在#162/#136另行登记范围后的后续实现，§10.10及T177为保留的历史来源，不将其未达波追认为通过。本轮实现及全部正式量测固定全新检出的clean `3ddf12d00c913e02217b5ebced90fc70b9ac723d`，Git tree `23895053afe1d1a4b93a386c6ca61e09aa5c66ce`；本节与公开证据摘要随后以独立报告提交追加，不把报告head冒充量测source。D0、10/50个不同授权subject、每档至少60秒有效请求跨度、所有波次AND、原SQL oracle/coverage/ID/阶段/字节守恒均保持，未升档。

修订针对负载端N线程唤醒后逐路发送的分散路径，不指定旧Ubuntu漏峰的GIL或内核原因：

- 主波次改为`selectors.DefaultSelector`；准备TCP/内存headers及精确reader ACK之后才开共同launch gate。共同gate不冒充N次开始，每socket首次真实`send`尝试独立计时，含零进度EAGAIN；从未调用send的失败仅留`client_attempt`，不补latency或在途。
- 每次有界write后重新处理read-ready；完整Content-Length正文和严格JSON解码后即记自身terminal，不等其他发送完成、所有响应、EOF或证据落盘。控制响应framer新增64KiB响应头、单响应及本波累计正文16MiB边界，原16MiB raw-wave证据限制仍保留，错误保留已知原始body；这是工具失败边界，不是放宽接受门槛。服务端业务、SQL、handler等待/队列未改，没有响应hold、sleep、线程优先级或GIL开关。
- `completion_journal.py`以N有界单写者持久化已完成记录，终态时钟先于enqueue；失败不重试可能已部分写入的raw，另保全全部已知pair的hashed fallback并令quality失败。它不把落盘CPU/墙钟开销隐去，资源与波间空档仍包括这些成本；不可取消的底层文件I/O卡住不在有界退出保证内。
- 既有5秒send总截止、5秒read无进展截止保留。正recv才延长read截止，EAGAIN不延长；最终send晚返回仍记实际已发送事实并判超时，不能切换预算绕过旧截止。仅非阻塞普通would-block记零字节进度；其他真实I/O/close错误仍失败，不补字节或EOF。

确定性反例要求：A完整响应已read-ready时，B仍短写/EAGAIN且未发完，必须先read/decode/complete A；同一ready批中的C下一次write也必须见A已完成。另覆盖ACK/连接失败不计starts、超时前后边界、partial-send中断与close所有权。真实认证/授权/HTTP/只读SQLite及四种流投影继续走原独立oracle；整链路写盘异常保留10/10已知调用且quality=false。合并定向回归110 passed/23.93s，dispatcher独立45 passed/1.86s；这些是开发回归，不冒充正式一分钟观察。

| 完整回归/来源 | 实际结果 |
|---|---|
| Windows11/AMD64，16逻辑CPU，pp03 Python3.12.15/SQLite3.53.4 | 新检出exact 3dd：1335 passed/15 skipped，225.79s，exit0；Black100、Pylint9.87、docrefs0、pipcheck0。原pp03四项dev/build依赖偏离锁，保留全局环境，以独立target overlay对齐后21/21核验通过再跑本行回归及正式观察。 |
| Ubuntu24.04.5/x86_64，16 vCPU，独立pp03 Python3.12.16/SQLite3.54.1 | 新clone exact 3dd、前后clean：1336 passed/14 skipped，176.59s；Black100、Pylint9.87、docrefs0、pipcheck0，21锁包0差异。 |
| RHEL9.7/x86_64，4 vCPU，独立pp03 Python3.12.16/SQLite3.54.1 | 新clone exact 3dd、前后clean：1336 passed/14 skipped，162.39s；Black100、Pylint9.87、docrefs0、pipcheck0，21锁包0差异。 |
| [CI740/run43342529840](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43342529840) | 四job success；3.11.18/3.12.16各1336 passed/14 skipped，92.91/93.32s；Black81+19、Pylint9.87、docrefs0。实际checkout为PR合成merge `0b75fababee55ab6487e4345c50b40df69139f20`，合source 3dd进base a10cab2f，分列于本地exact head回归；名为锁核验的步骤实际仅pip check，未据此宣称CI逐包精确版本复核。 |

Windows首次未对齐四项dev/build锁版本的回归同为1335 passed/15 skipped、227.70s，原记录保留为补充，不替代上表锁内最终回归。Linux多通过1项为POSIX SIGKILL恢复用例。两Linux69个源码文件及338个tracked原始字节前后均匹配，互相一致；Windows69个源码原始hash前后匹配，338项Git blob映射同一tree，未把CRLF字节与Linux LF冒称相同。`source_file_sha256`只指各机source.json文档字节，不是canonical代码hash；三机机器元数据不同，各自分列。

**当前判定：完整Gate0仍不通过。** 本轮Ubuntu两档全部达标，未复现旧未全重叠；RHEL有3个真实未达波，必须保留exit1。六档各一次，固定顺序Windows10→50→Ubuntu10→50→RHEL10→50，没有同source择优重测。完整公开白名单见[selector证据摘要](../tests/fixtures/query/evidence_162_20261113_selector_gate0.json)。

| 平台/档 | 达标/总波 | 主请求 | client/server峰范围 | executing独立范围 | quality/target/exit |
|---|---|---|---|---|---|
| windows-10 | 1242/1242 | 12420 | 10–10 / 10–10 | 10–10 | true/true/0 |
| windows-50 | 235/235 | 11750 | 50–50 / 50–50 | 44–50 | true/true/0 |
| ubuntu-10 | 388/388 | 3880 | 10–10 / 10–10 | 7–10 | true/true/0 |
| ubuntu-50 | 55/55 | 2750 | 50–50 / 50–50 | 20–50 | true/true/0 |
| rhel-10 | 859/861 | 8610 | 9–10 / 9–10 | 5–10 | true/false/1 |
| rhel-50 | 121/122 | 6100 | 50–50 / 49–50 | 20–50 | true/false/1 |

六档合计**2903波、45510主请求，HTTP错误0**，另120个前置请求。六档quality/时长/source/hash/逐连接字节检查均通过，2900/2903是达峰波数，不是HTTP成功率、新SLA或接受比例。接收→响应完成的服务端在途和所对应不同授权用户均按原阶段重算；executing不替代在途。

| 平台/档 | 有效/墙钟/空档（秒） | 全局主请求p50/p95/p99（秒） |
|---|---|---|
| windows-10 | 60.023070 / 186.541292 / 126.518222 | 0.040295 / 0.051775 / 0.079195，n=12420 |
| windows-50 | 60.119789 / 158.444145 / 98.324356 | 0.205411 / 0.292764 / 0.355990，n=11750 |
| ubuntu-10 | 60.113592 / 87.523715 / 27.410123 | 0.137622 / 0.191237 / 0.210524，n=3880 |
| ubuntu-50 | 60.345744 / 77.616405 / 17.270661 | 0.897131 / 1.257106 / 1.302503，n=2750 |
| rhel-10 | 60.020437 / 124.767376 / 64.746939 | 0.058739 / 0.080868 / 0.087950，n=8610 |
| rhel-50 | 60.414014 / 102.684960 / 42.270945 | 0.396189 / 0.619521 / 0.681203，n=6100 |

分位数取每档全部主请求的原始样本，采用全局(n−1)p线性插值，含未达波、不平均逐波分位数。client开始是每次真实非阻塞send尝试，terminal是完整read/decode；准备、证据drain及波间空档不填入有效时间。selector为Windows真实SelectSelector、两Linux真实EpollSelector。新客户端发起和落盘工作负载有变化，不据时间差宣称产品性能提升或操作系统排名。

Ubuntu10的388波first-send包装起点跨度p50/p95/最大为1.002159/1.493865/4.375909ms；首/次send间隔最大1.103749ms。这是新source客户端自身时钟的观察，不追认旧dba wave00024，不把包装起点当内核syscall entry。

| RHEL未达波 | client/server峰 | 同机客户端事实 | 同机服务端事实 |
|---|---|---|---|
| 10/wave-00661 | 9/9 | 首client完成比末send开始早1.736813ms；send起点跨度7.167265ms | 末received晚首response_completed 14.330604ms |
| 10/wave-00780 | 10/9 | send起点跨度0.827649ms；全部send完成早于首client完成14.082807ms | 末received晚首response_completed 1.457056ms |
| 50/wave-00070 | 50/49 | 全部send完成早于首client完成11.293777ms；末received对应第9次send的actor008 | 末received晚首response_completed 9.027070ms |

三波raw/report/index/hash、请求ID、网络双向字节、journal及原checks一致，无I/O错误或would-block。全部reader-installed及recv包装start均早于同serverACK；这不证明内核已阻塞。00661确有负载端仍未全发起即完成首响应；另两波客户端已全发完，不能一概归为发起不足。00780末请求recv包装返回至received间隔3.243036ms跨过首completion；50档末请求该间隔仅0.208864ms，较晚的是recv包装返回。当前证据不能指定GIL/内核/解析为根因，也不能推导产品只支持9/49用户。诊断白名单及原raw/report各自SHA在fixture分列。

后续可独立评估每次write后丢弃的writable键构建（正常10/50波约45/1225项）等热路径，但这只是源码可见的优化候选，未实施或据此宣称三miss修复。须先定位负载端发起及服务端read/解析/调度剩余间隙，再决定最小改动；不得延后read/响应完成、提前计received、变动阶段、扩队列或加试择优。

六份最终离线审核全部audit_valid=true/0问题，固定脚本SHA `a9df182c5e8ef17ce38b33d0d2872bc17707f42d0a07181a115cc2ddf2b59dfa`；13项离线变异自检通过，真实journal失败仍拒绝并保留部分审核边界。它重算hash/ID/峰值/准备/字节/投影/资源，不重查SQL或生成新负载。全局公开投影仍为990707d9…，前置实际page10 raw/consumed100窗及1告警、discard0；原v1重复流负例仍保留。全部归档每平台一个、各包含10与50两档，hash/大小/文件数在fixture；不是六个独立归档。

资源完整值见fixture，CPU为单波进程区间比率最大，RSS/FD/handle是采样下界而非精确峰；同机loopback两进程socket字节仅覆盖HTTP应用流，不含TCP/IP、重传或物理NIC。缓存未控制、无冷/热或稳态声明。原summary的remaining_gates为保守模板，其中两Linuxexact-source证据本节已补；其他真实Review、queued条件N/A/所选阶段及资源/cache充分性仍待B1/C4复核和A裁定。内部交叉核对不算独立批准。

旧T177的67未达、dba的Ubuntu1未达、新3dd的RHEL3未达及所有开发失败均保留；Windows selector初始空fd集合失败、发送截止绕过反例在冻结前修正，原日志不覆盖。本次不合并/关单、不升档，不以新Ubuntu通过冒称完整Gate0通过。


### 10.12 Gate0 READ-only polling candidate (2026-11-13)

Commit d4c5df3 removes a proven client cost: after each bounded write, a dedicated
READ-only selector avoids enumerating writable-only events from the mixed
selector (up to N(N-1)/2 unused events for N ready writers without readable data).
The main selector retains write/deadline scheduling. Responses are still read,
decoded and completed immediately. Both selectors close on registration errors,
interruptions and constructor failure. Windows still scans read descriptors;
this is not a capacity claim. Server code and acceptance thresholds are unchanged.

Windows verification with this checkout's src explicitly on PYTHONPATH:
49 dispatcher tests passed in 1.89s; query-tool regression 167 passed / 1 skipped
in 29.33s; Black and diff checks passed; dispatcher Pylint 9.29/10.
The first run imported the old installed checkout and returned 1 failed / 48
passed (field coverage mismatch). Its log is retained. Binding the current src
fixed that environment error; no test assertion or business schema was relaxed.

No new formal capacity run, full-suite or Linux run was performed. Previous
3ddf12d observations and all three RHEL misses remain failures. Two misses show
server-side timing gaps which this client optimization does not establish or
resolve. Full Gate0 remains unaccepted; 100k is unverified. Independent exact-head
review remains required. This is a local candidate, not a merged delivery.
