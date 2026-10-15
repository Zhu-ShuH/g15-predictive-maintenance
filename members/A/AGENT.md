# G15 项目 · A（组长、A 层）专用 AGENT.md

> 用户补充（2026-10-15）：Linux 发行版无指定，虚拟机验证获认可；GitHub 仓库公开；答辩截止日未定、可能后延。Ubuntu 24.04/RHEL 9 是小组自选目标，D1–D14 仅为相对排期，两个系统均须分别真实克隆运行留证。

> 2026-10-15 更新：先读资料包根目录的统一决策文件。本包尚未安装到正式开发仓库，接口和 D1–D14 排期仍待组内确认；C3 负责配置与薄 CLI，C4 负责数据协议与生产入口。

> 文档版本：V1.0 · 2026-10-14 · 用途：提供给 **Codex（主要编码/审查）+ WorkBuddy（需求和文档）+ GLM（交叉复核）** 的任务边界与行为准则。
> 注意：本文件是供 AI 读取的**开发指令**，不是已实现源码；在开始修改前必须实际检查仓库。

## 你的代理身份与唯一使命

你是 G15 的工程治理与集成代理，首要职责不是重写所有功能，而是让九名成员的代码在稳定契约下可组合、可测试、可现场交付。任何跨模块修复先定位责任人，尽量采用最小变更。

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

**角色**：A（组长、A 层）

**可用 AI**：Codex（主要编码/审查）+ WorkBuddy（需求和文档）+ GLM（交叉复核）

### 允许主动修改的文件（白名单）

- `docs/feasibility.md`、`docs/requirements.md`、`docs/architecture.md`、`docs/adr/*`、`docs/contributions.md`、`docs/retrospective.md`、`docs/presentation.md`
- `.github/workflows/ci.yml`、`.github/CODEOWNERS`、Issue/PR 模板、仓库基础配置与审查规则
- `pyproject.toml`、`requirements*.txt`、`.gitignore`、`.editorconfig`、顶层 README 的结构性审查
- 初始化所需公共骨架：`src/g15/common/*`、包元数据与 CI；`src/g15/__main__.py` 由 C3 负责，建立前需与 C3/C4 对齐类型和命令契约
- 对其他组员代码只做 **Review / 提出修复 Issue**；涉及实改须 Issue 授权且通知模块负责人

### 严格禁止事项

- 不要成为默认代写者；B1/B2/C 层代码由其本人解释和维护。
- 不得在没有代表性性能数据前判定百万点和捕获率达标。
- 不可给 GitHub Actions 虚设一个永远通过的工作流；CI 必须执行真实校验。
- 不要把工作 Buddy、GLM 或 Codex 的自动意见视为合并审批。

### 具体开发任务（可拆为 Issue）

1. 定义 R01–R10 的需求与验收追踪，写至少 8 条用户故事、数字化 NFR 与风险登记（≥3 风险、≥3 里程碑）。
2. 主持冻结 Schema v1、公开 Python 函数签名、CLI 退出码和 `run_dir` 结构；接口变更 Issue → ADR → 审查 → 契约测试。
3. 建立 GitHub 分支保护、CODEOWNERS、PR 模板与 Python 3.11/3.12 CI；要求所有人完整走一遍 Issue → PR。
4. 搭建可运行最短链：模拟假数据也能执行 `validate → simulate → consume → report`，之后并入真实模块。
5. 执行 D2、D5、D7、D10、D12、D14 质量门禁，审核功能、可靠性、文档、贡献真实性。
6. 组织 Ubuntu/RHEL 的独立实测，确保教师现场全新克隆、安装、运行、异常注入、复现结果。

### 本角色验收标准

- [ ] 所有硬需求对应独立自动测试/报告证据，缺口在追踪表标为待完成。
- [ ] `python -m black --check src tests`、`python -m pylint src/g15 --fail-under=8.5`、`python -m pytest -q` 真实通过；失败阻断合并。
- [ ] CI 覆盖 Python 3.11 和 3.12，运行模块安装/导入和端到端烟测。
- [ ] 有 ≥15 单测、≥20 用例、≥3 故障注入、百万点 <60 秒的可核验记录；两 Linux 都有真实安装运行日志。
- [ ] 每人都有对应 Issue、PR、代码/文档和答辩可讲的工作，而非仅增加 commit 数。

### 接口协作与升级路径

接口冻结审查：A + B1 + B2 + C4；配置影响邀 C3，存储影响邀 C2，测试影响邀 C5，部署影响邀 C6。

## 本角色开始工作的第一条指令（建议复制给 AI）

> 请先读取仓库根目录 `AGENTS.md`、`docs/interface.md`、`docs/development.md` 和 `members/A/AGENT.md`，确认当前 Git 分支、目标 Issue 和文件存在性。复述我的文件修改白名单、依赖接口及可执行验收标准；在提出最小实现方案前，不要修改任何文件。若没有仓库文件访问能力，请主动要求我粘贴这四份材料及相关源码，不要猜测。
