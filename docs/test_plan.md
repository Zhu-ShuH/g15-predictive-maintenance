# 测试计划：独立验证与报告（Issue #9）

> 负责人：B1（kittywent，2026-10-25 承接原 C5 职责）
> 依据：`docs/acceptance.md` §3/§4、`docs/interface.md` §5.3/§5.4/§8.5、`docs/development.md`
> 本文件是**计划**，实测结果见 `docs/test_report.md`。计划里写的任何数字都不是成绩。

## 1 验证目标与独立性

教师硬指标要求给出"受控故障检出率"，并且验证必须独立。本计划的独立性由三条约束保证：

1. 只有 `g15.reporting` 读 `private_truth/faults.json`；`simulation`、`pipeline`、`analysis`、`storage` 全程不读真值（见 §6.4 的可证伪用例）。
2. 匹配由验证器按 `device_id` 与事件区间自行计算，被测代码不产出 `event_id`，因此"检出"不是被测方自报的。
3. 验证 seed `20261016` 不参与阈值调参：`configs/health_rules.yaml` 在验证前已提交并记录摘要（§2），本轮未修改任何规则、阈值或测试期望。

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

## 4 匹配细则（`docs/acceptance.md` §3 的可执行化）

1. 真值事件是半开区间 `[s, e)`，仅验证器读取。
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
在 Linux 上用真实 `SIGKILL` 打断消费者，再重跑，并断言**库内容**与"未中断运行"一致。

用例 `test_kill_dash_nine_replay_matches_an_uninterrupted_run` 的做法：

1. 子进程**先启动一个监视线程**，轮询消费者自己写出的 `processed_chunks` 表；当该表出现第 1 行（即**第一片事务已提交**）时写握手文件，此时消费者正在处理后面的分片。握手点因此落在"至少一片已落库、整体尚未完成"的真实中间态，而不是在 `consume_run` 调用之前（固定 `sleep` 或调用前握手都能被快机器跑过去，证明不了任何事）。
2. 握手靠**观察数据库**取得，不往业务代码里加测试专用参数，生产路径保持干净。
3. 父进程轮询到握手才发信号；发 `SIGKILL` 后断言 `returncode == -SIGKILL`；若子进程自己跑完（`returncode == 0`）则用例失败，而不是静默通过。
4. 信号之后先断言库处于**部分提交**状态：`processed_chunks` 行数 `>= 1` 且 `< manifest.parts` 总数——否则说明信号打在消费开始前或结束之后，用例失败。
5. 重跑消费后比对 `runs`、`processed_chunks`、`feature_windows`、`alarms`、`alarm_states` 五张表的**全部内容**（含窗口几何、健康分、告警级别与持久化状态），不只比总行数——这样"写对行数但写错内容"的重放也会被抓到。

该用例在 Windows 上 **skip**，理由写进用例：`kill -9` 是 POSIX 语义，Windows 没有等价物，按 §4 必须在 Linux 留证，不在 Windows 伪造。

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
10. 工程门禁：`black --check src tests`、`pylint src/g15 --fail-under=8.5`、`pytest -q` 全部退出 0。

## 8 明确不覆盖（不得在答辩中宣称）

- **双 Linux 实测**：Ubuntu 24.04 与 RHEL 9 的全新克隆、安装、运行、`kill -9` 留证属 Issue #10（C6）。本计划所有数据均在 Windows 上取得，不能替代。
- **百万点 60 秒**：属生产基准（Issue #11 验收），本计划的受控轮廓只有 65536 点。
- **真实工业准确率**：本计划的"100%"只指 §3 那套预定义教学测试集上的事件召回率，不代表现场设备表现。
- **去抖/升级/恢复（N>1）**：受控轮廓固定 N=1，不能用它冒充去抖已验证。
- **C5 之外的模块单测**：各模块单测由各自负责人交付，本计划只统计真实跑过的总数（§5），不代写。
