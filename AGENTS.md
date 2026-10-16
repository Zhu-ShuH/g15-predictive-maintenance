# AGENTS.md — G15 项目全仓统一 AI 开发规则

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> 给所有在本仓库工作的 AI 编码助手（Codex、DeepSeek Harness、GLM 等）。这是全局约束；九位成员各自还有 `members/<role>/AGENT.md`。不保证所有工具自动读取本文件，必要时需用户显式提供。
> **仓库状态（2026-10-16）**：公共规则与角色文件已安装到正式仓库；主分支为骨架，业务 PR 尚在评审。先读根目录 00 文件；接口仍待组内评审，不得把草案或 CI 绿当作业务已通过。
> **用户确认（2026-10-15）**：Linux 发行版无指定，虚拟机验证获认可；Ubuntu 24.04/RHEL 9 为小组自选目标。GitHub 仓库公开；实际答辩截止日未定、可能后延。两套系统仍须分别全新克隆并运行留证。

## 必读顺序

1. `docs/interface.md`：待组内评审的架构、配置、数据与 API 契约；经 A 记录评审并合并到正式仓库后成为权威接口。
2. `docs/development.md`：质量、GitHub、测试、部署规范。
3. `members/<role>/AGENT.md`：你的用户分配的角色专属指令；不能自行切换角色。
4. 当前 GitHub Issue 及该功能现有代码、测试。

## 禁止偏离的系统契约

- 项目 G15 预测性维护场景沙盘；9 人，D1–D14 相对排期模板；完整链：YAML → NumPy 退化与故障 → NPZ/Manifest → 独立消费 → 窗口特征 → 0–100 健康分 → 告警 → SQLite → JSON/Markdown 报告。
- `schema_version=1.0`；分片固定 `seq:int64`、`timestamp_ns:int64`、`vibration:float64`、`temperature:float64`；Manifest 必须 SHA 校验且只有 `complete` 状态可消费。
- `private_truth/faults.json` 只能由验证器用于测试匹配；消费者及 analysis 禁止读真值。不得作弊伪造 100% 检出、忽略误报或捏造性能/测试结果。
- 消费 SQLite 写入使用同一事务，重放必须幂等；禁止在 CLI 实现业务公式；公共字段/签名/退出码修改必须 A 审核并更新 `docs/interface.md`。
- 教师 G15 指标：100 万点仿真 <60 秒、受控注入事件 100% 被检出、配置不改代码、接口完整；**生成+落盘**是小组组长确认的计时口径，具体点数和计时范围见 docs/acceptance.md。工程交付还需 ≥8 用户故事、≥15 单测、≥20 用例、≥3 故障注入；两种 Linux 实测是组长补充要求。

## 工作和输出流程

- 每次只做一个 Issue，只改该角色白名单文件；任何跨边界修改先征得 A 许可。
- 不要在看不到源码时臆造存在的模块/测试；先确认环境，再做最小正确实现、增加测试、运行可用的检查。
- 本地质量门禁：`python -m black --check src tests`、`python -m pylint src/g15 --fail-under=8.5`、`python -m pytest -q`；没运行就写未运行。
- 不得提交 `runs/`、`.venv/`、密钥、机器路径或大型原始数据；GitHub 通过 Issue→分支→PR→Review→CI，不直接改 main。
- 最终答复必须列出：Issue/完成内容、变更文件、真实测试证据、风险阻塞、PR 标题/下一步。

## 角色入口

`A` 组长；`B1` 仿真；`B2` 下游算法；`C1` 故障注入；`C2` 消费存储；`C3` YAML 与薄 CLI；`C4` Schema/NPZ/Manifest/生产入口；`C5` 独立验证、报告与测试汇总；`C6` 双 Linux 运维。每位模块实现者负责本模块测试。

> 有冲突时：停止越权改动，说明冲突和建议，等待 A 决定；不允许凭 AI 自己的偏好推翻工程基线。
