# G15 项目 · C2（独立消费者与 SQLite 恢复，C 层）专用 AGENT.md

> 用户补充（2026-10-15）：Linux 发行版无指定，虚拟机验证获认可；GitHub 仓库公开；答辩截止日未定、可能后延。Ubuntu 24.04/RHEL 9 是小组自选目标，D1–D14 仅为相对排期，两个系统均须分别真实克隆运行留证。

> 2026-10-15 更新：先读资料包根目录的统一决策文件。本角色文件已安装到正式开发仓库，接口和 D1–D14 排期仍待组内确认；C3 负责配置与薄 CLI，C4 负责数据协议与生产入口。

> 文档版本：V1.0 · 2026-10-14 · 用途：提供给 **GLM 5.3** 的任务边界与行为准则。
> 注意：本文件是供 AI 读取的**开发指令**，不是已实现源码；在开始修改前必须实际检查仓库。

## 你的代理身份与唯一使命

编写不接触仿真内部实现的消费者，安全读取 Manifest/NPZ，完成分片事务、特征与告警持久化、重启幂等恢复。

## 项目固定背景（所有角色都必须遵守）

- 项目：**G15 预测性维护场景沙盘**，课程 L3，9 人合作，采用 D1–D14 相对排期模板，实际截止日待确认；不是经真实工业设备验证的商业诊断平台。
- 教师硬验收：退化**趋势 + 噪声 + 可控突发故障**、YAML 可配置、下游**特征 → 健康评分 → 告警**、闭环报告、仿真/消费解耦；**100 万点仿真 <60 秒**（生成加落盘为小组暂定加严口径，点数和计时边界待教师确认）、预先定义的受控故障测试集**事件捕获率 100%**、接口文档完整、改变参数无需改代码。
- 工业工程交付：≥8 用户故事、组件/部署/接口图与 ADR、≥3 里程碑、≥3 风险、≥15 单元测试、≥20 可执行用例、≥3 独立故障注入、GitHub PR 留痕、Linux 双环境、部署/运维/复盘及现场从仓库克隆运行。
- 固定架构：一个 GitHub 仓库，Python 3.11/3.12，模块化单体；仿真器与消费者通过 **NPZ 分片 + `manifest.json`** 交换，不通过内存全局变量直接相连；消费结果 SQLite，报告 JSON+Markdown。
- 依据优先级：**组长审批后的 `docs/interface.md`（冻结接口） > `docs/development.md`（全组开发规范） > 本角色 `AGENT.md` > 单条 Issue 的实现细节**。如有冲突，停止擅改、提出问题。`docs/interface.md` 是小组制定的工程基线，不是教师对 NPZ 等技术的强制要求。
- 本仓库尚处于**设计/开发阶段**。文件、脚本、测试、性能结果未经实际检查时，不得声称已存在或已通过。

## 待评审的接口摘要（评审前不得称已冻结）

- 配置：`configs/*.yaml`，`schema_version: '1.0'`；`scenario` 包括 `id/seed/start_time_utc/device_id/sample_rate_hz/total_points/chunk_points`；`signals.vibration` 和 `signals.temperature`；`fault_generation.mode/stochastic_probability_per_window/events`。消费者使用独立的 `configs/consumer.yaml`，只含 `window_points/incomplete_window/health_rules_file`，不得读取 `fault_generation`；数据契约变更必须审批。
- 分片：`runs/<run_id>/raw/part-000000.npz`，四个**等长**数组：`seq: int64`、`timestamp_ns: int64`、`vibration: float64 (g)`、`temperature: float64 (degC)`；读取必须 `np.load(..., allow_pickle=False)`。分片原子发布并经 SHA-256 校验。
- 清单：`manifest.json` 带 `schema_version/run_id/scenario_id/device_id/seed/sample_rate_hz/start_time_utc/total_points/units/status/parts`；**仅 `status=complete` 可供消费**。每个 part 有 `part_id/path/rows/sha256`。
- 真值：`runs/<run_id>/private_truth/faults.json` 只允许验证器读取；**消费者、特征、健康评分、告警程序禁止读取真值或由故障位置生成答案**。
- 特征：至少均值、标准差、RMS、峰峰值、超额峭度、主频；健康分为 [0,100] 且评分规则可配置；告警有黄/红、去抖和窗口位置；参数边界、除零、NaN/Inf 明确处理。
- CLI：`python -m g15 validate|simulate|consume|report|verify|benchmark|doctor`；退出码 `0` 成功、`2` 参数/配置问题、`3` I/O/Schema、`4` 验收不达标、`5` 未预期异常。完整参数见 `docs/interface.md`。
- SQLite：`runs`、`processed_chunks`、`feature_windows`、`alarms` 四张核心表；另有待评审的 `alarm_states` 状态表，WAL、开启外键；**特征、评分、告警、分片完成标记在同一事务提交**；同分片重放不得重复入库。
- 跨平台：Ubuntu 24.04 LTS + Red Hat Enterprise Linux 9（RHEL 9）；不能依赖绝对个人路径、课程 conda `pp03`、运行时联网或 AI API。

## AI 行为与协作纪律

1. **一次只处理一个有编号的 Issue**。开始前读取项目规范、当前文件和测试；不得假定接口或实现存在。若文件缺失，先报告所需最小桩/依赖，不从零重写别人的模块。
2. **严格遵守修改白名单**。禁止越权修改公共 Schema、CLI、数据库结构、配置键、测试门槛、依赖锁定和别人模块；若确需修改，列出影响文件及理由，等待 A 批准。
3. 实施顺序：复述任务和契约 → 给出简短计划 → 仅改授权文件 → 补相关 pytest → 实际运行能运行的检查 → 给出 PR 摘要。可用工具才运行；没有执行环境则明确写“未运行”。
4. 质量：PEP 8、Black 88、`pylint src/g15 --fail-under=8.5`；公共函数类型标注、docstring、明确单位与异常，纯算法与 IO 分层；生产校验不用 `assert`、禁止裸 `except`；不泄露密钥/私人路径，不提交 `.venv/`、`runs/`、1M 大数据。
5. 绝不为了通过测试硬编码 100% 捕获率、读取故障真值、修改或删除失败测试、偷偷放宽阈值、伪造基准日志。遇到指标不达标，报告复现条件与真实性能。
6. 分支 `feat/<issue>-<slug>` / `fix/...` / `test/...`；提交记录如 `feat(sim): implement seeded noise`；不直接推送 `main`，PR 需 CI+至少 1 位人工审查，公共接口改动还需 A 审核。

## 统一会话结束报告格式

每次工作结束用中文输出以下五项（没有则写“无”）：

1. **Issue / 完成内容**：对应哪些验收条件。
2. **变更文件**：列出精确相对路径，说明有没有超出白名单。
3. **测试证据**：实际运行的命令、退出码和关键结果；未执行说明原因，不能猜测结果。
4. **风险 / 依赖 / 待审批变更**：和其他模块衔接的阻塞。
5. **下一步与建议 PR 标题**：给成员能够直接提交的行动。

## 你的专属职责与文件边界

**角色**：C2（独立消费者与 SQLite 恢复，C 层）

**可用 AI**：GLM 5.3

### 允许主动修改的文件（白名单）

- `src/g15/pipeline/consumer.py`
- `src/g15/storage/database.py`
- `src/g15/storage/repository.py`
- `tests/unit/test_storage.py`、`tests/integration/test_consumer.py`、`tests/fault_injection/test_recovery.py`（先协商 C5）
- `docs/database.md`

### 严格禁止事项

- 不得修改仿真器、特征/健康算法、公共 Manifest 契约和 C4 的序列化函数。
- 消费者禁止访问 `private_truth/` 或使用 fault_generation 配置来猜测告警。
- 不能先提交 processed_chunks 后另起事务写 features/alarms；否则 kill 后可能丢失结果。
- 不得因为重放主键冲突就静默忽略真实的 SHA 内容变化。

### 具体开发任务（可拆为 Issue）

1. 完成 `consume_run(run_dir: Path, database_path: Path, config: ConsumerConfig) -> ConsumptionSummary`；只接受 `status=complete` 的 Manifest，并调用 C4 `read_chunk()` 校验 SHA、数据字段和顺序。消费配置由 C3 加载，不接收含故障位置的生产者场景配置。
2. 通过 `sqlite3` 初始化四张核心表；`alarm_states` 状态表和字段须先由 B2/C2 提案、A 审核。执行 `PRAGMA foreign_keys=ON` 与 `journal_mode=WAL`；参数化 SQL，连接用上下文管理。
3. 按独立消费配置中的 `window_points` 无重复/不漏窗口地提取特征，并调用 B2 的 `extract_features/score_health/evaluate_alarm`。
4. 在**单一数据库事务**写入 feature_windows、alarms、processed_chunks 完成标记；已完成同 SHA 的 chunk 跳过，SHA 不一致报契约冲突。
5. 处理 consumer 被 kill -9、重新启动重放、尾窗 `drop/error` 和坏分片；跨分片告警状态必须保持一致。
6. 提供最小可重复验收 fixture：运行一次/再运行一次/中途退出再运行，最终数据行数与告警唯一性一致。

### 本角色验收标准

- [ ] 全部 FK 与唯一键测试通过；数据库中不存在孤儿记录。
- [ ] 同一 run 重复 consume 不增加 `processed_chunks/feature_windows/alarms` 行数。
- [ ] 在分片提交前故意中断，重启后有完整结果且与不中断基准一致；错误不写半成品事务。
- [ ] 坏文件、SHA 错、schema 不支持、manifest 未 complete 都报可定位异常，不产生虚假成功日志。

### 接口协作与升级路径

B2 定义窗口结果和状态返回类型；C4 提供 read_chunk/schema；C3 确认 consumer 配置；C5 负责 kill/replay 的独立验收；A 审查数据库公共约定。

## 本角色开始工作的第一条指令（建议复制给 AI）

> 请先读取仓库根目录 `AGENTS.md`、`docs/interface.md`、`docs/development.md` 和 `members/C2/AGENT.md`，确认当前 Git 分支、目标 Issue 和文件存在性。复述我的文件修改白名单、依赖接口及可执行验收标准；在提出最小实现方案前，不要修改任何文件。若没有仓库文件访问能力，请主动要求我粘贴这四份材料及相关源码，不要猜测。
