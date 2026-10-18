# G15 预测性维护场景沙盘：架构设计与接口契约 V1.0

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> 状态：**设计基线（待小组审核/实现）**。本文是项目册 G15 和课程 P4.1/P2.2 的小组技术落地方案；NPZ、SQLite、CLI、字段名、阈值等均为小组选定的工程约定，不是教师原文强制技术栈。组长审核通过后冻结 `schema_version=1.0`。若此前草案写的是 CSV 分片，**以本版 NPZ 分片为准**，CSV 仅可选导出。
> 2026-10-15 更新：先读资料包根目录 `00_先读_统一决策与待确认事项.md`；当前没有完成组内冻结或任何代码实测。下述 14 天为相对计划模板，实际截止日待确认。

## 0. 依据、目标和约束

项目册 G15 五项必验：①趋势+噪声+突发故障的退化仿真；②退化速率/故障率配置化；③下游特征→健康评分→告警；④闭环联调报告；⑤仿真消费解耦接口。G15 四项硬指标：**100 万点仿真 <60 s**、**受控注入故障 100% 被捕获**、接口文档完整、改参数不用改代码。关键技术：随机过程和 seed、管线集成、上下游时钟对齐。

项目册通用工业流程：≥8 条用户故事、数字化非功能要求、组件图/部署图/接口契约/ADR、WBS/≥3 里程碑/≥3 风险、≥15 单测、≥20 测试用例、≥3 次/类故障注入（明确按独立场景留证据）、自动化部署、运行手册、可观测/可恢复/配置外置、贡献与复盘。团队额外要求：GitHub **公开仓库**现场克隆可运行、两种不同 Linux 发行版独立验证。用户确认发行版无指定、虚拟机验证获认可；小组暂选 Ubuntu 24.04 LTS + Red Hat Enterprise Linux 9（RHEL 9）。D1–D14 是相对排期模板，实际答辩日期未定、可能后延；项目不是已在真实工厂验证的工业诊断模型。

## 1. 技术选型及边界

- 架构：**一个 Git 仓库 + 模块化单体 + 独立生产者/消费者命令**；不使用共享全局变量跨进程传数据。
- Python：支持 3.11 和 3.12；核心：`numpy`、`PyYAML`；测试：`pytest`；质量工具：`black`、`pylint`；持久化：标准库 `sqlite3`；接口：标准库 `argparse`、`json`；日志：`logging`。Pandas 按分析或导出需要选用，不是核心依赖。
- 内部数据交换：**NPZ 数值分片 + `manifest.json` 元数据 + Schema V1**。源数据与故障真值分目录，消费者在代码与接口层禁止读取真值；同一运行目录内的子目录划分不构成操作系统权限隔离。报告提供 JSON 和 Markdown；可选 CSV 导出用于人工检查。
- 没有强制 Web 界面、在线模型 API、MQTT、Redis、微服务、RUL 估计、机器学习训练。它们只能在验收主链达标后以受控 ADR 增补。
- SQLite 存储消费状态、特征、评分、告警和操作结果；`WAL` 模式，消费者单写者、按分片事务提交。
- 部署：源码安装到 Conda `pp03`（Python 3.12），支持离线运行（下载依赖属于安装阶段）；只依赖相对路径和环境变量。

## 2. 系统结构与模块依赖

```text
YAML 配置 → validate/config → Simulation Engine ─┬─> private_truth/faults.json（仅验证器）
                                               │
                                               └─> run/raw/part-000000.npz…
                                                       + manifest.json (SHA256, schema, status)
                                                                 │
                                                                 ▼
                                                   Consumer（独立进程）
                                                   ├─ 验证元数据/顺序/哈希
                                                   ├─ Feature Extractor（按窗口）
                                                   ├─ Health Scorer（0–100）
                                                   ├─ Alarm Rules（黄/红）
                                                   └─ SQLite（幂等事务）
                                                                 │
                                                                 ▼
                                                   Report Builder / Validator
                                                   ├─ JSON / Markdown
                                                   ├─ 故障真值匹配（仅测试）
                                                   ├─ 延迟/漏报/误报
                                                   └─ 性能/恢复证据
```

**依赖方向**：`cli → config/pipeline/report`；`simulation → common`；`pipeline → schema/serialization/analysis/storage`；`analysis → common`；`reporting → storage`。`simulation` **不得 import `analysis` 或 `storage`**；`analysis` **不得访问** `private_truth`。CLI 只能编排模块，不放业务公式。

## 3. 工程目录（唯一基线）

```text
g15-predictive-maintenance/
├── README.md
├── LICENSE                         # 授权方式由小组/教师确认
├── .gitignore
├── .editorconfig
├── pyproject.toml
├── requirements.txt               # 运行依赖固定版本
├── requirements-dev.txt           # 测试/质量依赖固定版本
├── configs/
│   ├── default.yaml
│   ├── consumer.yaml
│   ├── healthy.yaml
│   ├── degradation.yaml
│   ├── sudden_fault.yaml
│   ├── benchmark_1m.yaml
│   └── health_rules.yaml
├── src/g15/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── simulation/
│   │   ├── __init__.py
│   │   ├── engine.py
│   │   ├── degradation.py
│   │   ├── noise.py
│   │   └── faults.py
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── producer.py
│   │   ├── consumer.py
│   │   ├── schema.py
│   │   └── serialization.py
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── features.py
│   │   ├── health.py
│   │   └── alarms.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   └── repository.py
│   ├── reporting/
│   │   ├── __init__.py
│   │   ├── builder.py
│   │   └── metrics.py
│   └── common/
│       ├── errors.py
│       ├── logger.py
│       └── types.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fault_injection/
│   ├── performance/
│   ├── fixtures/
│   └── conftest.py
├── scripts/
│   ├── setup.sh
│   ├── demo.sh
│   ├── verify.sh
│   ├── benchmark.sh
│   └── clean.sh
├── docs/
│   ├── feasibility.md
│   ├── requirements.md
│   ├── architecture.md
│   ├── interface.md
│   ├── database.md
│   ├── test_plan.md
│   ├── test_report.md
│   ├── deployment.md
│   ├── operations.md
│   ├── contributions.md
│   ├── retrospective.md
│   ├── presentation.md
│   └── adr/
├── .github/workflows/ci.yml
├── .github/ISSUE_TEMPLATE/
├── .github/pull_request_template.md
├── .github/CODEOWNERS
└── runs/                            # 临时和运行产物，Git 忽略
```

## 4. 配置契约 V1

`configs/default.yaml` 的标准示例（初始值可调，但字段名和单位冻结）：

```yaml
schema_version: '1.0'
scenario:
  id: bearing_001
  description: '轴承缓慢退化与突发冲击'
  seed: 42
  start_time_utc: '2026-10-14T00:00:00Z'
  device_id: 'DEV-001'
  sample_rate_hz: 100
  total_points: 1000000
  chunk_points: 102400
signals:
  vibration:
    baseline: 0.5
    degradation_rate_per_sample: 0.000001
    noise_std: 0.05
    unit: 'g'
  temperature:
    baseline: 35.0
    degradation_rate_per_sample: 0.000003
    noise_std: 0.15
    unit: 'degC'
fault_generation:
  mode: manual             # manual | stochastic | combined
  stochastic_probability_per_window: 0.0
  events:
    - event_id: F001
      type: spike           # spike | step | burst
      start_seq: 600000
      duration_points: 256
      amplitude: 5.0
```

上例中的 `fault_generation` 只交给生产者。`consume` 不接收整份场景 YAML，也不把 `fault_generation` 对象传入下游。消费参数使用独立的 `configs/consumer.yaml`：

```yaml
schema_version: '1.0'
window_points: 256
incomplete_window: drop
health_rules_file: 'configs/health_rules.yaml'
```

消费者只读取公开 Manifest、NPZ 和该消费配置；`sample_rate_hz` 从 Manifest 取得。C3 与 B2/C2/C4 在接口评审时确认上述键名、路径解析与默认行为，增加测试证明消费配置没有故障位置。

**参数约束**：`total_points > 0`，`sample_rate_hz > 0`，`chunk_points > 0`，消费配置中 `window_points > 0`，并且 `chunk_points % window_points == 0`（非末尾分片按整窗口切分，避免崩溃时遗失跨片缓存）；退化率和噪声标准差应为有限非负数；故障概率 `0 ≤ p ≤ 1`；事件区间必须在总点数内，`start_seq ≥ 0` 且 `duration_points > 0`；不支持的故障类型必须明确报错；未知字段默认拒绝。生产与消费配置各自加载为经校验的不可变对象，不在各模块里重复读 YAML。两份配置的窗口整除关系在消费入口校验。

**随机性**：采用 `numpy.random.default_rng(seed)`；为不同设备和噪声/故障机制建立稳定的子随机流，避免算法修改导致无关随机数漂移。同配置、同 seed、同版本生成数值应逐点一致；`run_id` 和 wall-clock 运行时间允许不同。时钟基于起始 UTC + 采样序号计算，不以每循环累加浮点时间。

## 5. 数据交换契约 V1（冻结后禁止无审批修改）

### 5.1 分片 NPZ

每个 `runs/<run_id>/raw/part-000000.npz` 必须包含**同长度**数组：

| 字段 | 类型 | 说明/约束 |
|---|---|---|
| `seq` | `int64` | 从 0 开始；运行内严格递增；与分片连续 |
| `timestamp_ns` | `int64` | UTC Unix 纳秒；由 start + seq/sample_rate 算出 |
| `vibration` | `float64` | 单位 `g`；正常模式必须有限数 |
| `temperature` | `float64` | 单位 `degC`；正常模式必须有限数 |

`device_id` 和 `run_id` 在分片对应的 manifest 元数据中给出；一个运行只对应一个设备（多设备通过多个 run 支持，避免本期接口歧义）。消费者读取时必须 `np.load(path, allow_pickle=False)`；不得读取对象 pickle。边界分片可小于 chunk_points。类型或数值错误按消费者错误策略拒收并记日志。

**百万点口径已由组长确认**：按 `seq` 的采样次数计点；双信号的标量值总数另列，性能报告必须同时记录 `sample_count` 和 `scalar_value_count`，不能把两个值合并宣称为两个已验证采样点。

### 5.2 Manifest（示例）

```json
{
  "schema_version": "1.0",
  "run_id": "20261014T100000Z-bearing_001-a1b2c3",
  "scenario_id": "bearing_001",
  "device_id": "DEV-001",
  "seed": 42,
  "sample_rate_hz": 100,
  "start_time_utc": "2026-10-14T00:00:00Z",
  "total_points": 1000000,
  "units": {"vibration": "g", "temperature": "degC"},
  "status": "complete",
  "parts": [
    {"part_id": 0, "path": "raw/part-000000.npz", "rows": 102400, "sha256": "<64位十六进制摘要>"}
  ]
}
```

上面 `parts` 为**结构示意**，完整百万点运行应列出全部分片，且各 `rows` 之和与 `total_points` 一致。`status=complete` 才允许消费者开始；Manifest 最后原子发布。校验 SHA-256 与采样序号连续性。将分片先写入同目录 `.tmp` 文件，通过 `os.replace` 原子更名，随后发布清单；对于 `np.savez`，应传入打开的二进制文件句柄，以免其自动附加 `.npz` 扩展名。写入失败不得发布 complete 状态。

### 5.3 故障真值（只对验证器开放）

`runs/<run_id>/private_truth/faults.json`（小组暂定路径，非权限边界）：

```json
{"run_id":"...","events":[{"event_id":"F001","device_id":"DEV-001","type":"spike","start_seq":600000,"end_seq_exclusive":600256,"amplitude":5.0}]}
```

消费者不接收此文件路径、不读取 `private_truth`、不通过配置加载故障答案。真值与预测只在 `g15 verify` 阶段匹配，防止“看过答案”造成虚假的 100% 检出率。
如果验收要求实际访问权限隔离，则将真值移至独立根目录并调整 `verify` 参数与文件权限；迁移须先经接口评审。无论路径如何，增加测试证明消费流程仅使用公开数据和规则配置。

### 5.4 输出报告结构

`reports/summary.json` 最少包括：`run_id`、`schema_version`、`scoring_version`、`total_points`（采样次数）、`scalar_value_count`、`processed_points`、`rejected_points`、`feature_windows`、`alarm_count`、`health_score_min/max`、`elapsed_seconds`、`fault_events`、`detected_events`、`false_alarm_count`、`detection_rate`、`errors`。`reports/summary.md` 要有相同口径及运行环境。未知指标用 `null`，不得捏造 `0` 或 `100%`。

## 6. Python 模块公开接口契约

接口以函数签名与行为约定为准。实现可用 `dataclass`/`TypedDict` 描述类型；下面是约定接口，不代表代码已经存在。

```python
# config.py
load_config(path: Path) -> ScenarioConfig
validate_config(config: ScenarioConfig) -> None
load_consumer_config(path: Path) -> ConsumerConfig

# simulation/engine.py
simulate_chunk(config: ScenarioConfig, start_seq: int, count: int, rng: np.random.Generator) -> SimulationChunk

# simulation/faults.py
inject_faults(chunk: SimulationChunk, config: ScenarioConfig, rng: np.random.Generator) -> tuple[SimulationChunk, list[FaultEvent]]

# pipeline/serialization.py
write_chunk_atomic(chunk: SimulationChunk, output_path: Path) -> ChunkManifestEntry
read_chunk(path: Path, manifest_entry: ChunkManifestEntry) -> SimulationChunk

# pipeline/producer.py
produce_run(config: ScenarioConfig, output_root: Path) -> Path  # 返回 run_dir

# analysis/features.py
extract_features(vibration: NDArray, sample_rate_hz: int) -> FeatureRecord

# analysis/health.py
score_health(features: FeatureRecord, rules: HealthRules) -> HealthResult

# analysis/alarms.py
evaluate_alarm(health: HealthResult, state: AlarmState, rules: HealthRules) -> tuple[list[Alarm], AlarmState]

# pipeline/consumer.py
consume_run(run_dir: Path, database_path: Path, config: ConsumerConfig) -> ConsumptionSummary

# reporting/builder.py
build_report(run_dir: Path, database_path: Path) -> Path
```

**设计约束**：特征、评分、告警规则尽量使用无副作用的纯函数；文件 I/O 仅位于 pipeline/storage/reporting；不跨模块 import 私有 `_name`；不在 import 时执行仿真或访问文件。不要把 `assert` 用于生产输入校验，应使用 `ValueError`/专门异常并映射到明确的 CLI 退出码。

## 7. 仿真模型规范

### 7.1 常规信号（确定性趋势 + 随机噪声）

针对采样序号 `i`，可使用可解释的初始模型：

- `vibration(i) = v0 + kv*i + εv(i)`
- `temperature(i) = t0 + kt*i + εt(i)`
- 噪声 `ε ~ N(0, σ²)`，从固定 `seed` 生成。

参数要写清物理量纲；此模型是教学用退化过程假设，而不是设备真实物理规律。`float64` 向量化生成，分片长度例如 100,000；不得 Python 层逐点 `append` 百万次。数值较大时须防止溢出、负值或不合理物理值，处理策略需文档声明。

### 7.2 注入类型

- `spike`：一次/多次短时脉冲；
- `step`：某时间点发生幅度阶跃；
- `burst`：一定持续窗口的强震动波动。

手动事件必须可指定类型、起点、持续点数、强度；随机故障需按可配置概率发生并以 seed 复现。不同注入事件重叠时，规定“加性叠加”或“优先级”策略并测试。

### 7.3 性能评价

必须实测：**1,000,000 点、从配置校验后、首次数据生成前计时到全部 NPZ 数据分片和 complete Manifest 写入完成（含随机初始化、序列化、哈希与原子发布），详见 docs/acceptance.md，elapsed <60 s**。使用 `time.perf_counter()`；报告 CPU、内存、Python/NumPy 版本、Linux、块大小、数据量、产生速率及磁盘写入是否包含在内。测试脚本在 Ubuntu 和 RHEL 独立执行。不要把 CI 共享 Runner 波动时间当作唯一结论。

## 8. 特征、评分、告警规范

### 8.1 窗口处理

消费者按 `window_points` 聚合原始采样，例如 256 点一个窗口。非末尾分片要求 `chunk_points` 是 `window_points` 的整数倍，**窗口不能因为分片切换而丢点或重算**；生产者必须保证这一约束。最后不足窗数据依 `incomplete_window` 策略丢弃或报错，并统计。

### 8.2 至少 5 个特征

- `mean`：均值；`std`：标准差；`rms`：均方根；`ptp`：峰峰值；`kurtosis_excess`：超额峭度（正态基准 0）；`dominant_freq_hz`：主频（Hz）；按工作量可补 `crest_factor`、频带能量比例。
- 零方差或零均值时，所有除法使用明确的边界处理，禁止产生 NaN/Inf 且继续入库。
- `rfft` 单边幅值计算须正确处理 DC/Nyquist 频点（不应无条件乘 2）；主频搜索是否排除直流必须写在规则文档。

### 8.3 健康评分（启发式，可解释）

为特征 `f_i` 定义基线 `b_i`、容许增量 `r_i >0`、权重 `w_i≥0` 且 `Σw_i=1`：

`risk_i = clip((f_i - b_i)/r_i, 0, 1)` （方向相反的特征使用对应风险映射）

`health = clip(100 * (1 - Σ w_i*risk_i), 0, 100)`。

所有基线、阈值、权重从 `configs/health_rules.yaml` 读取；版本 `scoring_version` 写入结果。该公式不是剩余使用寿命预测，也不证明真实设备故障诊断精度。

### 8.4 告警

建议黄级阈值、红级阈值及“连续 N 窗口触发”均配置化；规则应包括异常原因、触发指标和窗口位置。同一运行/设备/规则/窗口有唯一告警键；支持恢复状态和防抖。健康分变低时告警级别不能倒挂。

**冻结前必须补齐的状态契约**：B2 与 C2 共同定义告警连续计数、升级/恢复和跨分片状态如何持久化或从已提交窗口可靠重建；C2 保证状态与窗口、告警、`processed_chunks` 在同一事务提交。A 审核后再实现 `kill -9` 恢复，不能只依赖进程内存状态。

### 8.5 故障捕获率的严格口径

`detection_rate = matched_injected_fault_events / total_injected_fault_events`。

事件匹配及受控配置按 docs/acceptance.md；验证器按 `device_id` 和事件区间进行匹配；允许的检测时延 `max_detection_lag_windows` 必须测试前写明。正常场景的误报数量同时报告。应准备至少正常、缓慢退化、突发冲击、阶跃故障四种场景，并预留未参与调参的验证 seed。**“100%”只指预设受控注入测试集**，不能宣称真实工业环境无漏报。

## 9. SQLite 数据库契约

核心表（字段可扩展，唯一键和事务不可省略）：

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  scenario_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  scoring_version TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL
);
CREATE TABLE processed_chunks (
  run_id TEXT NOT NULL,
  part_id INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  PRIMARY KEY (run_id, part_id),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE feature_windows (
  run_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  start_seq INTEGER NOT NULL,
  end_seq_exclusive INTEGER NOT NULL,
  features_json TEXT NOT NULL,
  health_score REAL NOT NULL CHECK(health_score BETWEEN 0 AND 100),
  PRIMARY KEY (run_id, device_id, start_seq),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE alarms (
  run_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  start_seq INTEGER NOT NULL,
  level TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, device_id, rule_id, start_seq),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE alarm_states (
  run_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  last_end_seq_exclusive INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  PRIMARY KEY (run_id, device_id, rule_id),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
```

`PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;`（在数据库连接/初始化环节执行并验证）。主键和外键语义需测试。所有写入使用 `with connection:` 事务；**特征/健康分/告警/分片完成标记在同一事务内提交**。遭遇 kill -9，未提交事务自动回滚；重启时从 `processed_chunks` 判断需重放的输入，依唯一键避免重复结果。若同名 `part_id` 的 SHA-256 被改变，必须拒绝重放并报契约冲突，不得静默覆盖。
`alarm_states` 是跨分片告警状态的**待评审候选表**，用于将连续计数、当前等级和恢复进度随分片一起提交。B2/C2 须明确 `state_json` 的版本和字段、`last_end_seq_exclusive` 的连续性检查，A 审核后才实现；不得仅保存进程内状态。

## 10. CLI 契约和退出码

统一入口为 `python -m g15`，安装成功后可额外提供 `g15` 命令：

```bash
python -m g15 validate --config configs/default.yaml
python -m g15 simulate --config configs/sudden_fault.yaml --output-root runs
python -m g15 consume --run-dir runs/<run_id> --config configs/consumer.yaml
python -m g15 report --run-dir runs/<run_id>
python -m g15 verify --run-dir runs/<run_id>
python -m g15 benchmark --config configs/benchmark_1m.yaml
python -m g15 doctor
```

输出给人的摘要可以是中文，但结构化 JSON 字段、CLI 参数、路径和返回码不使用中文命名。退出码：`0` 成功；`2` 参数/配置不合法；`3` I/O 或 Schema 验证失败；`4` 验收门禁失败；`5` 未预期错误。命令必须支持 `--help` 和非交互模式。**上述 CLI 是待实现的接口规格，不是已经运行的命令。**

## 11. 报告与运行目录

```text
runs/<run_id>/
├── manifest.json
├── raw/part-000000.npz ...
├── private_truth/faults.json     # 仅验证使用
├── artifacts/results.sqlite
├── reports/summary.json
├── reports/summary.md
├── logs/app.jsonl
└── evidence/                   # 性能/故障/双系统的运行证据
```

日志建议按每行 JSON 记录：`timestamp_utc, level, run_id, component, event, part_id?, elapsed_ms?, error_code?, message`。遇到坏数据、缺文件、重复、恢复、低健康分等事件都要可查。不得输出 AI 密钥、GitHub token、绝对私人路径。运行目录、`.venv` 和大数据都加入 `.gitignore`，仓库只提交小样例 fixture。

## 12. 跨平台部署与故障恢复

Linux 验证目标由小组自选为 Ubuntu 24.04 LTS + Red Hat Enterprise Linux 9（RHEL 9）；用户确认两种发行版无指定且虚拟机验证获认可。须在两套不同发行版的虚拟机或系统中分别从公开 GitHub 仓库全新克隆，并确认 Conda pp03 内实际安装 Python 3.12。RHEL 虚拟机先确认可用软件仓库及所需包，再执行安装，不沿用原目标发行版的软件源假设。按 environment.yml 创建或复用 Conda pp03 / Python 3.12，并安装项目依赖，版本不符停止且不覆盖旧环境；详情见 docs/environment.md。依赖尚未锁定，后续由 A/C6 验证。`setup.sh` 在明确的版本要求和依赖错误下退出非零状态；`demo.sh` 从空 `runs/` 可重建结果；`verify.sh` 检查测试、完整链路和验收门禁；`benchmark.sh` 生成可审计的性能结果。脚本使用 `set -euo pipefail`、相对目录定位和有意义的退出码。

必须进行真实故障注入：①业务突发故障检出；②文件损坏/哈希失败隔离；③消费进程 `kill -9` 后重启并重放；可选 ④无权限写入、⑤非法配置。**断电保证**要区别于进程强杀：`os.replace`、WAL、SHA 能提高可靠性，但不能未经断电实测就声称零数据损失。所有演示保留命令和日志。

## 13. 接口变更流程

`docs/interface.md` 是接口事实来源。变更 `schema_version`、公共模块签名、CLI 参数、配置字段或健康评分口径时：

1. 提 Issue，说明现状、原因、兼容影响、迁移计划。
2. A + B1 + B2 + C4 审查；根据影响范围邀请 C2/C3/C5。
3. 修改接口文档、测试 fixture 和消费端，再修改实现。
4. 运行 `bash scripts/verify.sh` 并在两种 Linux 至少做兼容烟测。
5. 经评审合并；破坏兼容时提升 schema 的主版本。

任何人和任何 AI 助手都不得未经此流程擅自“优化”公共字段和阈值。

## 14. 需求—验收追踪（起始版）

| ID | 用户故事 / 需求 | 模块 | 证据 |
|---|---|---|---|
| R01 | 正常、退化、突发故障预设 | configs + simulation | 运行三配置 + 测试 |
| R02 | 修改退化速率/故障率不用改代码 | config | 配置 A/B 对比 |
| R03 | 同 seed 可复现 | simulation | 两次生成数值对比 |
| R04 | 100 万点 <60 秒 | simulation + pipeline | benchmark 机器实测 |
| R05 | 消费端与仿真器独立 | pipeline | 分开进程调用 |
| R06 | 特征→健康分→告警 | analysis | 特征/评分/告警测试 |
| R07 | 受控故障 100% 检出 | verify | 真值对账+误报统计 |
| R08 | 闭环联调报告 | reporting | JSON+MD 报告 |
| R09 | 重复消费不重复入库、kill 后恢复 | storage/consumer | 重放测试 |
| R10 | 新环境 GitHub 克隆，两种 Linux 可运行 | scripts/CI | 双 Linux 安装运行记录 |

新增需求必须携带验收用例；没有可检查证据的需求不能标为“完成”。
