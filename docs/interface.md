# G15 预测性维护场景沙盘：架构设计与接口契约 V1.0

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> 状态：**设计基线（待小组审核/实现）**。本文是项目册 G15 和课程 P4.1/P2.2 的小组技术落地方案；NPZ、SQLite、CLI、字段名、阈值等均为小组选定的工程约定，不是教师原文强制技术栈。组长审核通过后冻结 `schema_version=1.0`。若此前草案写的是 CSV 分片，**以本版 NPZ 分片为准**，CSV 仅可选导出。
> 2026-10-15 更新：先读资料包根目录 `00_先读_统一决策与待确认事项.md`；当前没有完成组内冻结或任何代码实测。下述 14 天为相对计划模板，实际截止日待确认。

> **接口修订待审（T74，2026-10-24）**：本分支将 #57/#61 的持久化与单片责任提案落实为可审阅的正文差异，关联 #2/#65。前置 #68 上界勘误已经独立审阅合入 main 12e35e9c；本 PR 的其他接口修订仍须相关角色审阅；该修订未合并前不改变 main 的接口基线，不代表相关成员已确认或 Linux 兼容已通过。规则/阈值、Manifest 字段与 SQLite 表列保持；新增显式重置命令及公开签名继续单独评审。

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

**依赖方向**：`cli → config/pipeline/report`；`simulation → common`；`pipeline → common/schema/serialization/analysis/storage`；`analysis → common`；`reporting → storage`。`simulation` **不得 import `analysis` 或 `storage`**；`analysis` **不得访问** `private_truth`。CLI 只能编排模块，不放业务公式。

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

**参数约束**：`total_points > 0`，`sample_rate_hz > 0` 且 `sample_rate_hz <= 1_000_000_000`（上界依据见 5.1「采样率上界」），`chunk_points > 0`，消费配置中 `window_points > 0`，并且 `chunk_points % window_points == 0`（非末尾分片按整窗口切分，避免崩溃时遗失跨片缓存）；退化率和噪声标准差应为有限非负数；故障概率 `0 ≤ p ≤ 1`；事件区间必须在总点数内，`start_seq ≥ 0` 且 `duration_points > 0`；不支持的故障类型必须明确报错；未知字段默认拒绝。生产与消费配置各自加载为经校验的不可变对象，不在各模块里重复读 YAML。配置配对必须保证**名义分片尺寸** `chunk_points % window_points == 0`；消费端在业务结果写入前核对公开 Manifest 中所有非末片 `rows` 一致且可被 `window_points` 整除。**单片责任采用方案 C 提案**：单片只有末片，现有 Manifest 没有名义 `chunk_points`，消费者不能据公开输入验证该名义值的整除关系；配置配对检查由调用方保证，消费者不得将不可执行的检查报告为“已通过”。消费者仍只读公开 Manifest、NPZ 与独立消费配置，不加载生产场景 YAML 或 `fault_generation`。末片实际长度不要求被 `window_points` 整除；真实末片余数按 `drop/error` 处理，少于一窗且 `drop` 时可成功而没有特征/告警。

**多片末片上界校验现状（#76 已集成）**：主线`b3daf15ee8d53d7515f32b13b57de149f6767561`已包含C2的#76修复。消费端由一致非末片rows推断名义片长，在业务结果写入前拒收`last.rows > inferred_chunk_points`，错误点名part、rows与推断上界；等长整除末片允许，单片仍按方案C，不凭空反推名义片长。该上界预检失败时本次调用不新增业务提交；对新数据库实测五张业务表均为0行，既有数据库的历史提交不删除；此结论不扩大为任何消费失败都会全run回滚，合法短尾的drop/error仍沿既有策略。§5.1含等号的生产契约继续保持；本次只同步已集成实现，不新增Manifest字段、配置键或公开签名。

**真实末片丢点示例**：`configs/benchmark_1m.yaml` 的 `total_points=1000000`、`chunk_points=102400`，独立消费窗为 256 时，末片 78400 行满足 `78400 = 306 × 256 + 64`；公开入口实跑得到 `windows_written=3906`、`windows_dropped=64`、10 片。该结果在受测树 `82bafa4`（与 main `12e35e9c` 的 src/configs 相同）复验，没有修改 seed 或规则。这 64 点是**合法末片不足窗的 drop 策略结果**，不是“末片超过名义分片”的证据。此前在未含#76的受测树，另行公开API探针形状`[128,256]`、窗64被接受并产生6窗，曾证明末片超长拒收缺失；该历史行为现已由#76修复为业务写入前拒收。合法基准的64点仍按既定drop策略统计并打印，不把它修饰成0。正式运行的配置配对证明由调用方留证；此责任澄清须按 §13 审阅，不能用它放宽正式验收门槛。

**随机性**：采用 `numpy.random.default_rng(seed)`；为不同设备和噪声/故障机制建立稳定的子随机流，避免算法修改导致无关随机数漂移。同配置、同 seed、同版本生成数值应逐点一致；`run_id` 和 wall-clock 运行时间允许不同。时钟基于起始 UTC + 采样序号计算，不以每循环累加浮点时间。

## 5. 数据交换契约 V1（冻结后禁止无审批修改）

### 5.1 分片 NPZ

`SimulationChunk` 唯一定义位于 `g15.common.types`；`g15.simulation.engine`
保留同对象重导出。四数组均为一维、等长；内存空块允许，文件空分片非法。
生成端检查 dtype、等长、片内 seq 连续及信号 finite；序列化写端独立重查并拒空片；
读取端核对 SHA、rows 后重查数组；消费入口负责 part 顺序、跨片连续性与运行完整性。
这些检查不得因上游已验证而省略。数值生成和单位含义维持当前基线。

每个 `runs/<run_id>/raw/part-000000.npz` 必须包含**同长度**数组：

| 字段 | 类型 | 说明/约束 |
|---|---|---|
| `seq` | `int64` | 从 0 开始；运行内严格递增；与分片连续 |
| `timestamp_ns` | `int64` | UTC Unix 纳秒；由 start + seq/sample_rate 算出 |
| `vibration` | `float64` | 单位 `g`；正常模式必须有限数 |
| `temperature` | `float64` | 单位 `degC`；正常模式必须有限数 |

`device_id` 和 `run_id` 在分片对应的 manifest 元数据中给出；一个运行只对应一个设备（多设备通过多个 run 支持，避免本期接口歧义）。消费者读取时必须 `np.load(path, allow_pickle=False)`；不得读取对象 pickle。类型或数值错误按消费者错误策略拒收并记日志。

**短尾片（`total_points` 不是 `chunk_points` 整数倍）**：`chunk_points` 是名义分片大小，不是整除要求，生产者不得因为不整除而拒绝配置，也不得为凑整而补点。分片数 `n = ceil(total_points / chunk_points)`；前 `n-1` 片每片 `chunk_points` 行，**最后一片为 `total_points - (n-1) * chunk_points` 行**（因此必然满足 `1 ≤ rows ≤ chunk_points`）。注意上界是**含等号**的：当 `total_points` 恰为 `chunk_points` 的整数倍时 `ceil` 不进位，末片行数**等于** `chunk_points` 而非小于它；只有 `total_points` 不是整数倍时末片才是真正的短尾片（`rows < chunk_points`）。消费端若据非末片推导名义分片大小并据此校验末片上界，必须用 `<=` 判定，用 `<` 会误拒合法的整除 run。短尾片在其余方面与普通分片完全相同：`part_id` 仍从 0 连续、`rows` 与文件内实际行数一致、SHA-256 覆盖实际字节、Manifest 的 `rows` 之和仍等于 `total_points`。短尾片不得与前一合并、不得丢弃、不得用重复或填充采样补齐。`docs/acceptance.md` 第 2 节规定的基准形状正是这种情形：1000000 点、`chunk_points=102400` 得到 10 片，即 9×102400 + 78400。

**时间戳可表示范围**：`timestamp_ns` 为 `int64` UTC Unix 纳秒，上限 `2**63-1`（约 292 年跨度）。生产入口在创建运行目录之前用整数运算核算 `start_time_utc + total_points / sample_rate_hz` 是否会越界，越界则以 `ValueError` 拒绝，不写出任何分片或清单；生成端不得依赖 `int64` 回绕后的结果。

**采样率上界**：`sample_rate_hz <= 1_000_000_000`（1 GHz，即相邻样本间隔不小于 1 ns）。该上界是**契约约束，不是性能守卫**：

- `timestamp_ns` 是 `int64` 纳秒计数，最短可表示间隔为 1 ns；采样率更高时采样周期不足 1 ns，相邻样本被舍入成同一个时间戳，`np.diff` 恒为 0，而长度、dtype 与 `isfinite` 检查全部照常通过。
- 向量化路径的中间量 `remainder * 10**9`（`remainder <= sample_rate_hz - 1`）在采样率超过 `2**63 // 10**9 = 9223372036` 时于 `int64` 上回绕，函数不报错而返回错误甚至为负的时间戳。取 1 GHz 作上界同时使该乘积可证不溢出（不超过 `10**18 - 10**9`）。

三处校验必须一致：生产配置校验（`g15 validate` / `config.py`）、`load_scenario_parameters` 与 `timestamp_ns_for` 都拒绝超过上界的采样率并抛 `ValueError`。**不允许出现「配置能通过校验、内核却拒绝」的落差**；`config.py` 的同步由 C3 负责。

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

#### 5.2.1 读入侧校验规则（Manifest 是不可信输入）

Manifest 可能被手工编辑、由旧版本写出，或被中断的进程留在半成品状态，因此读入侧**不得以“能解析”推断“可信”**。以下四条为冻结规则，`g15.pipeline.schema` 与 `g15.pipeline.serialization` 是它们的可执行形式（Issue #34 收口）。

1. **`start_time_utc` 是完整且锚定 UTC 的时刻。** 形如 `YYYY-MM-DDThh:mm:ss[.ffffff]`，其后必须带时区（`Z` 或 `±hh:mm`）且偏移为 **0**。`Z` 与 `+00:00` 等价，**两者都接受**——生产者会把配置文本原样写入清单，而配置校验接受零偏移，读入侧比生产者更严会让一次运行被自己的读者拒绝。**`-00:00` 一律拒绝**：按 RFC 3339，它的含义是「本地偏移**未知**」，与「确认是 UTC」不是同一件事；清单声明的是运行开始时刻，偏移未知的时刻支撑不了这个声明。仅日期（`2026-10-14`）、无时区、非零偏移（如 `+08:00`）、非法日历值（如 `2026-13-45T99:99:99Z`）以及任意文本一律拒绝。不要只检查结尾的 `Z`，也不要只调用 `fromisoformat`：后者会把 `2026-10-14` 静默当成当日零点，且会把 `-00:00` 与 `+00:00` 一视同仁。

2. **`part_id` 必须与文件名编码的序号一致。** `path` 先满足安全相对 POSIX 路径规则，随后**只取** `PurePosixPath(path).name` 比对：若它匹配 `part-NNNNNN.npz`（**六位及以上**，如 `part-000000.npz`、`part-1000000.npz`），其数字必须等于 `part_id`；`part_id=0` 配 `raw/part-000005.npz` 必须拒绝。父目录不冻结（`raw/` 是约定而非约束），非规范名（如 `raw/shard-4.npz`）不含序号，故不做比对。

3. **`status` 决定 `parts` 的下限。** 只有 `complete` 可被消费，且必须**至少列出一个分片**、`rows` 之和恰好等于 `total_points`；`failed` **允许零分片**（表达“第一个分片落盘前就失败”），也允许部分分片，但分片仍须从 0 连续编号。任何情况下都不得把 `failed` 当 `complete` 消费。

4. **分片归属由 `run_dir` 决定，不由文件名决定。** `read_chunk(path, entry)` 只比对文件名与 SHA-256，字节完全相同的副本可以通过；因此**消费入口必须传 `run_dir=`**，此时解析后的分片必须恰好位于 `run_dir / entry.path`，越界（含符号链接逃逸）一律拒绝。省略 `run_dir` 的调用仅做文件身份/摘要/字段校验，只可用于已知归属的上下文。

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
MAX_SAMPLE_RATE_HZ: int = 1_000_000_000  # 配置层公开上限，与内核同值，见 5.1
load_config(path: Path) -> ScenarioConfig
validate_config(config: ScenarioConfig) -> None
load_consumer_config(path: Path) -> ConsumerConfig

# simulation/engine.py
MAX_SAMPLE_RATE_HZ: int = 1_000_000_000  # 采样率契约上限，见 5.1
simulate_chunk(config: ScenarioConfig, start_seq: int, count: int, rng: np.random.Generator | None = None) -> SimulationChunk
timestamp_ns_for(start_seq: int, count: int, start_time_utc_ns: int, sample_rate_hz: int) -> NDArray[np.int64]

# simulation/faults.py
inject_faults(chunk: SimulationChunk, config: ScenarioConfig, rng: np.random.Generator | None = None) -> tuple[SimulationChunk, list[FaultEvent]]

# pipeline/serialization.py
write_chunk_atomic(chunk: SimulationChunk, output_path: Path, *, run_dir: Path | None = None, part_id: int | None = None) -> ChunkManifestEntry
read_chunk(path: Path, manifest_entry: ChunkManifestEntry, *, run_dir: Path | None = None) -> SimulationChunk

省略 `run_dir` 只做文件身份/摘要/字段校验，**消费入口必须传**（见 §5.2.1 第 4 条）；`write_chunk_atomic` 的 `run_dir` 用于归属校验、`part_id` 用于显式指定分片编号，二者省略时按调用方默认行为处理。

# pipeline/producer.py
produce_run(config: ScenarioConfig, output_root: Path) -> Path  # 返回 run_dir

# analysis/features.py
extract_features(vibration: NDArray, sample_rate_hz: int, *, window_start_seq: int | None = None) -> FeatureRecord

# analysis/health.py
score_health(features: FeatureRecord, rules: HealthRules) -> HealthResult

# analysis/alarms.py
evaluate_alarm(health: HealthResult, state: AlarmState, rules: HealthRules) -> tuple[list[Alarm], AlarmState]

# pipeline/consumer.py
consume_run(run_dir: Path, database_path: Path, config: ConsumerConfig) -> ConsumptionSummary

# reporting/builder.py
build_report(run_dir: Path, database_path: Path) -> Path
```

`simulate_chunk` 与 `inject_faults` 的 `rng` 仅为调用契约兼容保留，接受
`None` 或 `numpy.random.Generator`，非法类型拒绝；不得消费调用方生成器状态。
确定性由配置 seed、稳定子流与绝对样本索引决定，不能声称实际使用了传入 RNG。
随机故障的事件调度规则仍按 #6 单独评审，不由共享类型迁移补造。

`simulate_chunk` 的 `count` 可为 `0`：在配置与 `start_seq` 均合法时，
这是合法的空块调用，返回四个长度为 `0` 的一维数组；`seq`、`timestamp_ns`
仍为 `int64`，`vibration`、`temperature` 仍为 `float64`。空块不产生采样点，
也不消费调用方生成器状态；`count < 0` 仍须拒绝，`count=0` 不跳过输入校验。
此约定只描述仿真函数调用，不允许 `total_points=0` 的生产运行，也不允许
把空块作为 NPZ 分片发布；生产配置和分片非空约束继续按 §4、§5 执行。

**设计约束**：特征、评分、告警规则尽量使用无副作用的纯函数；运行数据与结果的文件 I/O 位于 pipeline/storage/reporting；现有 `analysis.health.load_health_rules(path)` 读取规则 YAML 是已知配置加载例外，不读取运行数据或真值，算法计算本身仍不读文件；不跨模块 import 私有 `_name`；不在 import 时执行仿真或访问文件。不要把 `assert` 用于生产输入校验，应使用 `ValueError`/专门异常并映射到明确的 CLI 退出码。

### 6.1 消费窗口、存储与 CLI 的责任

消费入口从公开样本的绝对 `seq` 传入 `window_start_seq`；离线单窗特征/评分可省略它，进入告警链时必须有窗口位置。`end_seq_exclusive = start_seq + window_points`，但该算式不能代替真实 seq 连续性检查。consumer 检查首片从 0 开始、跨片首尾连续、跨窗无空洞/重叠，恢复时从已提交位置继续；每次 `read_chunk` 传入 `run_dir=`。非末片整除与单片不可推断边界见 §4，只有真实末片可应用不足窗策略。

`consume_run(run_dir, database_path, config)` 返回的 `ConsumptionSummary` 包含 `run_id/device_id/windows_written/alarms_written/shards_processed/shards_skipped/windows_dropped`。`windows_dropped` 沿用既有名称，单位是被丢弃的**样本点数**，不是窗口数；改名会破坏既有调用接口，本次不改名；如需改名，按 §13 接口变更流程办理。重复消费已提交同 SHA 的片只跳过分析，不丢失已提交状态。

SQL 与计算留在 storage/consumer；CLI 只加载配置、解析参数、选择既定输出路径和映射错误码。`consume` 薄接线已由 #71 合入主线；现有公开重置能力仍在 `ConsumptionRepository.reset_alarm_state`，consumer 级包装签名、审计返回与新增 CLI 子命令仍由 #65 后续提出，不把 `consume` 合入误写成显式重置已经交付。

## 7. 仿真模型规范

### 7.1 常规信号（确定性趋势 + 随机噪声）

针对采样序号 `i`，可使用可解释的初始模型：

- `vibration(i) = v0 + kv*i + εv(i)`
- `temperature(i) = t0 + kt*i + εt(i)`
- 噪声 `ε ~ N(0, σ²)`，从固定 `seed` 生成。

参数要写清物理量纲；此模型是教学用退化过程假设，而不是设备真实物理规律。`float64` 向量化生成，分片长度例如 100,000；不得 Python 层逐点 `append` 百万次。数值较大时须防止溢出、负值或不合理物理值，处理策略需文档声明。

噪声固定按 `BLOCK_POINTS = 16384` 寻址，块 b 覆盖
`[b*16384, (b+1)*16384)`，子流标签为 `vibration` / `temperature`。
播种使用 `default_rng(SeedSequence([seed & 0xFFFFFFFFFFFFFFFF,
crc32(stream.encode("utf-8")) & 0xFFFFFFFF, b & 0xFFFFFFFFFFFFFFFF]))`，
按绝对下标截取块内噪声；不得改用进程不稳定的 `hash()`。
相同配置、代码与依赖版本下，结果与分片尺寸、生成顺序、重试无关。
`BLOCK_POINTS` 是数值契约，`chunk_points` 是落盘分片参数，二者不等同。
修改块大小、标签或播种映射须走兼容性评审：提升相应版本或明确声明跨版本数值不保证一致，
不得静默改动后仍宣称同版本逐点一致；本次不修改算法、常量或 schema_version。

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

**#73 拟议的 analysis `FEATURES_VERSION="1.1"` 接受域（待该候选合入后成为主线事实）**：

- 采样输入只接受显式实数 dtype 白名单：`float16/float32/float64` 与 NumPy 标准有/无符号整数宽度；先把非原生字节序归一为本机字节序，因此大端和小端只改变存储编码，不改变类型判定。复数、`bool`、`datetime64`、`timedelta64`、`longdouble` 及对象等其他 dtype 明确抛 `ValueError`，不得先强转后静默丢信息。
- 整数逐值判断能否精确表示为 `float64`；不能精确表示时抛 `ValueError` 并点名 dtype/值。`2**53` 不是一刀切上界：例如 `2**53+1` 不可精确表示而拒收，`2**53+2` 可精确表示而允许。
- 计算二阶、四阶矩时可按 2 的幂定标以避免中间溢出/下溢；`mean/std/rms/ptp` 返回前必须全部有限，真实结果超出 `float64` 可表示范围时抛 `ValueError` 并点名字段，不用 `nan_to_num` 伪造有限值。普通幅值保持公式与舍入级等价，但不承诺跨 NumPy 版本逐位相同。
- `numpy.ma.MaskedArray` 的掩码语义不在 #73/#66 范围；当前候选仍可能丢弃掩码，另由 #82 先按 §13 评审。不得把上述 dtype 白名单写成“已支持掩码数组”。

### 8.3 健康评分（启发式，可解释）

为特征 `f_i` 定义基线 `b_i`、容许增量 `r_i >0`、权重 `w_i≥0` 且 `Σw_i=1`：

`risk_i = clip((f_i - b_i)/r_i, 0, 1)` （方向相反的特征使用对应风险映射）

`health = clip(100 * (1 - Σ w_i*risk_i), 0, 100)`。

所有基线、阈值、权重从 `configs/health_rules.yaml` 读取；版本 `scoring_version` 写入结果。该公式不是剩余使用寿命预测，也不证明真实设备故障诊断精度。

### 8.4 告警

建议黄级阈值、红级阈值及“连续 N 窗口触发”均配置化；规则应包括异常原因、触发指标和窗口位置。同一运行/设备/规则/窗口有唯一告警键；支持恢复状态和防抖。健康分变低时告警级别不能倒挂。

**持久状态与兼容边界（#57 提案）**：`AlarmState` 八键为 `active_level`、`consecutive_breaches`、`normal_windows`、`alarm_windows`、`last_window_start_seq`、`recovered_at_seq`、`scoring_version`、`rule_id`。计数是非负 JSON 整数，不能接受布尔值；位置按可空整数与非负校验；窗口推进的单调性由 analysis 层的 `evaluate_alarm` 保证，解码层不承诺已检查两个位置之间的相对大小或跨次倒退。带可继承历史时，规则身份与最后窗口位置必须可核且相容，不把缺身份历史当作新空状态，不回填无从证明的来源。

当前存储信封为 `state_schema_version="1"`、`state`、`reset_event`，有历史重置时可附 `reset_history`。正常恢复缺版本、未知版本、缺/多状态键或非法类型均明确拒收；包括无历史 legacy 的自动迁移目前未实现，不能承诺“空旧行自动续跑”。B2/C2 仍须逐项确认类型与迁移口径，不能只凭算法层拒收就声称数据库恢复安全。

显式重置与正常恢复分开：操作者重置留旧原文、原因和 UTC 时刻，后续状态写入及重开库不得抹掉审计，多次重置按最旧到最新保留历史。现有实现允许显式归档不可解码原文，是否将未来版本/损坏 JSON 纳入公共运维策略须受影响角色明确评议；`allow_legacy=False` 的严格路径保留。CLI 对应入口及确认参数仍按 #65 单独审阅。

**重置 streak 不等于重新分析 run**：该动作不清除旧结果/`processed_chunks`，不更新 `runs.scoring_version`，不能作为变更规则/窗宽后“整 run 重算”的承诺。当前更换分析配置应保留原库、向新数据库消费；任何覆盖旧库或新增选择参数必须另行明确操作和审计。不得在消费失败时自动重置。

C2 保证状态与窗口、告警和 `processed_chunks` 按片同事务提交，重启只采用已提交态；真实 `kill -9` 与双 Linux 恢复证据仍须另外交付，不能仅依赖进程内存或异常注入测试。

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
`alarm_states` 已在 #64 消费/存储实现落地，承载跨分片状态；公共 JSON 与兼容口径按 §8.4/§9.1 本次提案审阅，不把实现合入等同于全组契约冻结。`last_end_seq_exclusive` 是提交进度锚点，正常连续窗口与最后起点/窗宽一致；显式重置可清空 streak 但保留消费进度，须区分这两类状态。

### 9.1 JSON 信封、追溯与重放

上述五表、字段、主键和外键不因 JSON 信封新增或改变：

- `features_json` 采用 `features_version/features/sample_count/sample_rate_hz/window_start_seq/scoring_version/rule_id/health_explain` 信封。`features` 是八个特征的映射，不是新增八列；`HealthResult.contributions` 不直接落库。保存的原特征须与相应规则版本一起解释，报告不能拿当前规则替代历史规则而宣称重现原评分。
- 当前 `features_version="1"` 是 storage 的信封编码版本（`FEATURES_SCHEMA_VERSION`）；analysis 的 `FEATURES_VERSION` 是另一个常量（当前值见该模块；#73 候选拟由 `1.0` 升为 `1.1`，未合入前不写成主线已升级）。两者含义不同，不能用前者宣称每行已记录算法版本。算法/规则内容的完整追溯标识仍是 #57 的开放决定，不在本 PR 新增未实现字段。
- `alarms.created_at` 是消费落库 UTC RFC3339 `Z` 时间，不是数据采样时刻或真值事件时间；run/device 来源于公开 Manifest。
- `runs.status` 镜像 Manifest 状态，不代表消费已完成；完成进度由已提交 `processed_chunks` 与 Manifest 对照。相同 run 的已记录元数据/评分版本与已有窗口宽度冲突时拒收。
- 同片同 SHA 重放跳过分析、保留已提交状态；同 ID 异 SHA 拒收。片事务成功后才推进内存状态，失败/重启从提交态恢复，不继承未提交的计算结果。
- 当前对同版本号规则内容、`incomplete_window` 变化没有完整持久化指纹/自动检测，不能声称已验证全部分析身份。变更配置须保留旧结果并在新数据库重算；C2 提出指纹/兼容方案后按 §13 更新，不能静默扩 schema。本条披露限制，不把未实现的检测写成验收通过。

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

输出给人的摘要可以是中文，但结构化 JSON 字段、CLI 参数、路径和返回码不使用中文命名。退出码：`0` 成功；`2` 参数/配置不合法；`3` I/O 或 Schema 验证失败；`4` 验收门禁失败；`5` 未预期错误。命令必须支持 `--help` 和非交互模式。**上表是统一接口规格，不代表每个子命令都已实现；其中 `consume` 已由 #71 合入主线，其余命令按各自实现与验收记录判断。**

### 10.1 consume 的编排与可见性

数据库路径沿用 §11 的 `run_dir/artifacts/results.sqlite`，不新增 `--database` 或生产场景输入参数。参数/消费配置错误映射 2，Manifest/分片/数据库 I/O 或兼容错误映射 3，未预期错误映射 5，正常完成与同配置幂等重放返回 0；#71 当前实现已由 C2/C3 对当前 head 确认公开签名与实际异常集合适配，不能再宽泛捕获后返回成功。

退出码 `4`（验收门禁未达标）供 `verify` 及约定使用该码的验收入口表达未达标结果，不用于普通 `consume`；`consume` 的失败分类为 `2`/`3`/`5`。这是返回码的职责划分，不表示当前验证器或验收脚本已实现该分支。

若本次 `shards_processed > 0` 且 `windows_dropped > 0`，stderr 必须给出丢弃样本数；同片全 skip 且无本次丢点不误报警。0 窗口成功不是“已完成故障检出验收”。`reset-alarms` 的公开函数/参数/审计输出未在此冻结，consume 失败绝不自动触发重置。

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

## 15. Issue #17 接口评审与迁移记录（2026-10-18）

- 来源：[C4 意见](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/17#issuecomment-6247508474)、
  [B1 完整回复](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/17#issuecomment-6247619901)。
  这是线上逐项反馈，未虚构会议、全员批准或验收通过。
- 各议题的原文立场与落点分别记录，不将责任分工建议写成对另一位评审者的附议：

  | 议题 | B1 原文立场 | C4 原文立场 | 本次落点 |
  |---|---|---|---|
  | 唯一公共定义 | 同意唯一公共定义及同对象重导出 | 建议唯一类型，推荐迁往 common，不另造同名类型 | common.types |
  | 四数组 dtype / 单位 | 逐项确认 int64/int64/float64/float64 与 g/degC | 未逐项确认字段与单位 | §5.1 |
  | 内存空块 | 允许，并提供 count=0 实测 | 未明确表态内存空块 | 允许，仅存在于内存 |
  | 文件空分片 | 拒绝 | 建议拒绝，并说明序列化侧已拒收 | §5.1 拒绝落盘 |
  | 校验责任分层 | 给出生成、写入、读取、消费四层表 | 给出独立校验与跨片职责分发建议 | §5.1，保留各自来源 |
  | rng | 提出方案 (a)：可选参数、不得消费调用方状态 | 表述为“实现可声明不消费” | A 采纳 B1 方案 (a)，落定 §6 措辞 |
  | BLOCK_POINTS 入契约 | 同意，并提供值、播种规则与建议文本 | 未明确表态固定块入契约 | §7.1 |

  B1 提出方案 (a)（保留 None 默认值并显式不得消费），C4 表述较弱；
  A 采纳 B1 的方案 (a)，不将两种原文意见写成一致附议，提交 B1/C4 复审。
- 迁移只移动 SimulationChunk，engine 保留同对象导出；ScenarioConfig 与 FaultEvent 不迁移。
  `engine.SimulationChunk is common.types.SimulationChunk` 成立；现有从 engine 导入的调用点
  （包括 B1 仿真/性能测试及 C4 序列化运行时导入）无需改动即可继续取得同一类对象。
  测试路径是该兼容约束的下游，不是兼容保证本身。C4 的 serialization 当前仍局部从 engine 导入，重导出只能兼容，
  不会自动消除 pipeline→simulation 依赖；应由 C4 在 #3 范围改为 common，
  或先点名补充 #17 范围。本 PR 未越范围改该文件。
- 本记录描述待复审的变更与兼容约束；PR 未合并前不声称 main 已迁移或接口已全组冻结。
  完整生产百万点、检出率与双 Linux 仍按 acceptance.md 独立验收。
