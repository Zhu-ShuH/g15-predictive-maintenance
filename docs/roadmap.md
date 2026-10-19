# G15 项目开发全程指南（九人、十项 Issue）

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> 本文状态：草案／待评审；适用对象：组长 A 与 B1、B2、C1–C6；更新日期：2026-10-18。
>
> D1–D14 为**相对工作日排期模板**，不是已确定的日历截止日期。Issue #1 已合并且关闭；Issue 与 PR 的新增、关闭及指派状态以 [项目任务列表](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues) 为准。#12 由 PR #14 跟踪，#13 跟踪脚本文件模式；**指派情况会随邀请被接受而变化，请以 Issue 页面为准**。本页写的是目标和门禁，**不是已实现/已通过记录**。main `2c14759` 已合入 PR #15 的仿真内核及测试；公共契约待决项按 #17 等任务评审，完整主链、正式生产性能、受控检出率和双 Linux 尚未验收。依赖尚未锁定，不能称为可复现交付。

**术语**：波次是按依赖安排的并行开发批次；里程碑是某一天必须拿出可核对证据的阶段目标；门禁是达不到就暂停进入下一阶段的条件；Schema 是约定数据有哪些字段、类型和规则的协议；Manifest 是记录分片位置、行数和校验摘要的清单；NPZ 是保存多个 NumPy 数组的归档格式；np.savez 不压缩，压缩写入是另一种选项；stub 是为了确认模块能接通而临时使用的最小占位实现，**不是完成业务功能**；ADR 是记录架构选择及取舍的决策文件。PR 是合并前的审查请求，CI 是自动检查。

**命令约定**：除特别说明，下面的 `git` 命令在 Windows Git Bash、Windows PowerShell 与 Linux 终端相同，且需在仓库根目录执行。Windows 与 Linux 的 Python 虚拟环境解释器分别是 `python`、`python`。当前 `scripts/*.sh` **全为未实现占位**且 Git 索引权限 `100644`：Linux 克隆后不可直接执行；未来即便脚本实现，文档和部署命令仍写 `bash scripts/setup.sh`、`bash scripts/demo.sh` 等，不写 `./scripts/*.sh`。脚本可执行位问题已由 A 单独开 Issue 交 C6 用 `git update-index --chmod=+x scripts/*.sh` 修复，本次文档不修改索引。

## 0. 先认清基线、边界与判定方法

1. 领取任务前先看自己分支、仓库跟踪文件和设计基线；不要把设计示例当成已经可调用的代码：

   ```sh
   git status --short --branch
   git ls-files 'docs/interface.md' 'docs/development.md' 'members/*/AGENT.md' 'src/g15/*' 'tests/*' 'scripts/*'
   ```

   ✅ 成功标志：能确认文件确实存在；当前 main 除 `tests/test_package_import.py` 外，已有三份仿真单测和 `tests/performance/test_simulation_benchmark.py`；其余主链尚未联通；`docs/interface.md` §4–§12 和 §14 是**待评审的拟定契约**。

2. 识别当前失败与未来目标的差别。当前 `src/g15/__main__.py` 会以“CLI 未实现”退出 `1`；`scripts/setup.sh`、`demo.sh`、`verify.sh`、`benchmark.sh`、`clean.sh` 都报告“未实现”并退出 `1`。不要把 `docs/interface.md` §10 的合法配置 `0`、非法配置 `2` 写成现状。只检查占位文件位置，不要求新人跑失败命令：

   ```sh
   git ls-files 'src/g15/__main__.py' 'scripts/*.sh' 'configs/*.yaml'
   ```

   ✅ 成功标志：知道哪些文件是“存在但不可用”；是否真正达到目标只看未来的实跑命令、退出码、日志和报告，不能只看空文件或 CI 绿勾。

## 1. 三波推进：先协议，再核心，最后独立验证与部署

| 波次 | 谁 | 做什么 | 为什么这个顺序 |
|---|---|---|---|
| 第 1 波（第 1–2 天） | A、C4、C3 | 文档与评审记录；NPZ/Manifest 数据协议；配置与 validate 命令 | 地基，别人都要用 |
| 第 2 波（第 3–5 天） | B1、C1、B2、C2 | 仿真、故障注入、特征/评分/告警、消费与存储 | 依赖第 1 波契约 |
| 第 3 波（第 6–10 天） | C5、C6、C4 | 独立验证与报告、双 Linux 部署、生产入口 | 依赖前面跑通 |

**并行例外不改变波次**：B1 的百万点基准脚本和 C6 的两台 Linux 虚拟机都要**第 1 天**启动准备；C4 的 #3 在第 1 波，#11 正式生产入口在第 3 波。第 1 波的“接口评审记录”是待做事项，不能把当前设计基线称作已冻结。第 2 波成员可先做不改变契约的分析与小测试，依赖未就绪时不得把临时 stub 宣称为真实集成。

1. 每人每天先同步共享 `main`、在自己任务分支工作；Git Bash / Linux：

   ```sh
   git switch main && git pull
   git status --short --branch
   ```

   PowerShell：

   ```powershell
   git switch main
   if ($?) { git pull }
   git status --short --branch
   ```

   ✅ 成功标志：`main` 已更新且无未处理冲突；工作区有改动时先保留并查明来源，不强制切分支。

## 2. 九人业务 Issue 清单（#2–#11 共十项，另有 #12 文档与 #13 文件模式任务）

以下“允许修改”是 **Issue 本次任务**范围；`members/<角色>/AGENT.md` 是该角色**长期**文件边界，两层都要看。表中的相对文件名都以仓库为根；`analysis/*` 等指对应 `src/g15/` 下的目录。表格列出各任务的验收要求，不表示全链已经通过；已合入的仿真模块证据见 PR #15，完整验收仍待集成。测试路径按每个 Issue 正文逐项列明；已授权但不存在的文件可以在该 Issue 中新建。PR #15 已于 2026-10-17 合并（`2c14759`），仿真实现、测试及点名 ADR/报告已在 main；授权范围仍以 Issue 正文为准。额外路径仍需明确授权。

| # | 标题 | 负责人 | 允许修改的文件（本次 Issue） | 验收命令／条件（未来目标） | 依赖 |
|---|---|---|---|---|---|
| #2 | 需求、架构与 ADR 文档 + 接口评审记录 | A | `docs/feasibility.md`、`docs/requirements.md`、`docs/architecture.md`、`docs/adr/*` | ≥8 用户故事、≥3 里程碑、≥3 风险；一份评审记录 | 无 |
| #3 | NPZ 分片与 Manifest 序列化契约 | C4 | `src/g15/pipeline/schema.py`、`src/g15/pipeline/serialization.py`、`tests/unit/test_serialization.py` | 原子落盘、SHA-256 校验、`allow_pickle=False`、坏哈希被拒 | 无 |
| #4 | 配置加载校验 + validate 命令 | C3 | `src/g15/config.py`、`configs/*.yaml`、`src/g15/cli.py`、`src/g15/__main__.py`、`tests/unit/test_config.py` | `validate` 合法配置退出码 0；非法配置退出码 2；未知字段被拒；**当前 CLI 未实现，实测为 1** | 无 |
| #5 | 仿真内核（退化趋势 + 噪声） | B1 | `src/g15/simulation/engine.py`、`src/g15/simulation/degradation.py`、`src/g15/simulation/noise.py`、`tests/unit/test_simulation*.py`、`tests/performance/test_simulation_benchmark.py`、`docs/adr/0001-simulation-model-and-determinism.md`、`docs/adr/b1-issue5-report.md` | 同 seed 逐点一致；第 1 天先出百万点基准脚本（计入正式测量时须含写盘） | #3 |
| #6 | 故障注入（spike/step/burst） | C1 | `src/g15/simulation/faults.py`、`tests/unit/test_faults*.py` | 可指定起点/时长/幅度；真值清单一致 | #5 |
| #7 | 特征 / 健康评分 / 告警 | B2 | `src/g15/analysis/features.py`、`src/g15/analysis/health.py`、`src/g15/analysis/alarms.py`、`configs/health_rules.yaml`、`tests/unit/test_analysis*.py` | ≥5 特征；健康分 ∈[0,100]；常值窗口不产生 NaN/Inf | #4 |
| #8 | 消费入口与 SQLite 存储 | C2 | `src/g15/pipeline/consumer.py`、`src/g15/storage/database.py`、`src/g15/storage/repository.py`、`tests/unit/test_consumer*.py` | 同一事务提交；重复消费不重复入库；`kill -9` 可重放 | #3、#7 |
| #9 | 独立验证与报告 | C5 | `src/g15/reporting/builder.py`、`src/g15/reporting/metrics.py`、`tests/integration/*`、`tests/fault_injection/*`、`docs/test_plan.md`、`docs/test_report.md` | 仅验证器读真值；报告给检出率与误报数；输出 `summary.json`/`.md` | #6、#8 |
| #10 | 部署脚本与双 Linux 准备 | C6 | `scripts/setup.sh`、`scripts/demo.sh`、`scripts/verify.sh`、`scripts/benchmark.sh`、`scripts/clean.sh`、`docs/deployment.md`、`docs/operations.md` | `setup.sh` 按 `environment.yml` 创建或复用 `pp03`（Python 3.12）；demo/verify/benchmark 可执行；第 1 天先起两台虚拟机 | 无 |
| #11 | 仿真生产入口 producer | C4 | `src/g15/pipeline/producer.py`、`tests/unit/test_producer*.py` | 产出完整 `run_dir`（分片 + 完整 Manifest） | #3、#5 |

**测试文件如何补**：实现者应按角色指令编写本模块测试；若对应文件未列入该 Issue 的“修改范围”，先在 Issue/PR 留言请 A 确认本次授权，**不能因为它在长期白名单内就直接越权提交**。A 于 2026-10-16 明确把 PR #15 的点名性能测试与两份 ADR/报告加入 #5；该补充不等于整个 tests/performance/ 或 docs/adr/ 获得任意修改权。ADR 定稿由 A 负责，B1 在 #5 只草拟点名仿真文件，不修改 #10 的脚本。跨角色协作同理，改 `docs/interface.md`、评分公式或公共 CLI 需单独审批。

## 3. 高风险项：第 1 天就开跑准备，不等第 3 波

1. **B1：百万点基准脚本**。先与 C4/C5 核对 #3 数据协议、计时范围和脚本归属，再在获准的本 Issue 路径准备测量。正式验收按 1,000,000 个 `seq` 采样点，计生成 **+ NPZ 分片写盘 + 完整 Manifest 落盘**；同时记录双信号的标量值数，计时起止点以 docs/acceptance.md 为准。核对 YAML/脚本与已交付基准测试；脚本仍是占位，但性能测试已由 PR #15 提供：

   ```sh
   git ls-files 'configs/benchmark_1m.yaml' 'scripts/benchmark.sh' 'tests/performance/test_simulation_benchmark.py'
   ```

   ✅ 成功标志：确认 main 的 YAML/脚本仍待实现、基准测试文件已存在（默认跳过，需显式启用）；局部内核基准不等于完整生产验收。实测记录机器、系统、CPU、Python/NumPy 版本、seed、块大小、耗时和是否写盘。未运行就写“未运行”，不报 `<60s` 已达标。

2. **C6：两台 Linux 虚拟机**。第 1 天先准备小组自选的 Ubuntu 24.04 LTS 与 RHEL 9 环境，分别确认 Python 3.11/3.12 与包源；不把“已启动虚拟机”当作“全新克隆跑通”。在**每台 Linux** 中运行：

   ```sh
   git --version
   conda run -n pp03 python --version
   # 机器只装 3.12 时，把上一行改成 python3.12 --version
   ```

   ✅ 成功标志：两台机器各有独立版本记录；不满足版本就先解决环境并如实登记阻塞。等 #10 实现后才在全新克隆上按 `bash scripts/setup.sh`、`bash scripts/demo.sh`、`bash scripts/verify.sh`、`bash scripts/benchmark.sh` 分别执行留证；**现在这些命令均为退出 1 的占位，不应作现阶段验收命令**。

## 4. 里程碑、放行门禁及未通过时怎么做

| 里程碑 | 未来门禁（保持原计划数字） | 必须保存的证据 | 未通过时 |
|---|---|---|---|
| M1（第 2 天） | 接口评审有记录、最小 stub 链路可跑通 | 评审参与人/决定/待办记录；最小链路实际命令、退出码、日志 | 当前**待完成、未验证**；不进入依赖未经批准的正式接口开发；若 stub 未通，记阻塞而非伪称已完成 |
| M2（第 5 天） | 正常/退化/突发三场景可独立生产 + 消费 | 三套不同进程运行记录，run_dir 的 Manifest/NPZ、数据库与错误日志 | 优先修主链和接口，不扩新场景 |
| M3（第 10 天） | 百万点基准、检出率、误报、幂等、`kill -9` 恢复有真实证据 | 性能环境与计时、预设受控故障真值匹配、正常误报数、重复消费行数、强杀重启对照 | 不声明验收通过，保留失败记录，集中修复 |
| M4（第 14 天） | Ubuntu + RHEL 全新克隆跑通、文档齐全、答辩演练完成 | 两台机器分别的安装/运行命令、退出码、运行结果、演练记录 | 暂停发布，补可复核证据；实际日期由组长另行确认 |

这些是**未来目标**，不是当前状态。教师的百万点 <60 秒与预设受控事件 100% 捕获要求要真实测试；小组将“生成 + 落盘”列为**组长确认的计时口径**，不写成教师规定了落盘边界。受控测试集的 100% 不能推广为真实工业设备无漏报。`docs/interface.md` §10 的合法配置退出 `0`、非法配置退出 `2` 为待实现的契约；当前 `validate` 实测退出 `1`、报 CLI 未实现。`status=complete` 的 Manifest、`summary.json`、`summary.md` 都是待实现产物，不提供现在声称可成功的闭环命令。

1. 每个门禁复核真实检查命令。在完成 pp03 安装并激活后，Windows Git Bash / PowerShell：

   ```sh
   python -m black --check src tests
   python -m pylint src/g15 --fail-under=8.5
   python -m pytest -q
   ```

   Linux：

   ```sh
   python -m black --check src tests
   python -m pylint src/g15 --fail-under=8.5
   python -m pytest -q
   ```

   ✅ 成功标志：**只执行所属平台一组**，逐条记录退出码；`0` 只证明对应命令完成，骨架期就算全绿也不能替代 M1–M4 的业务、性能、双系统证据。未亲自运行写“未运行”，不能编数字。

## 5. 每日节奏、接口审批与集成方式

- **每天** 15 分钟站会：昨天证据、今天 PR、阻塞与所需审批；先同步 `main` 再开工。每 **2–3 天**演示一次真实 `simulate → consume → report`，仅在三入口实际实现后执行；当前该链路未实现，演示任务为“待完成”。
- 每个 PR **最新 CI 全绿 + 至少 1 人真人 Review**；涉及 Schema、配置键、CLI 的 PR **必须 A 审**。跨文件改动需评审记录，不能因代理建议而自行重写公共函数签名。
- CI 在 Python 3.11/3.12 运行安装、Black、Pylint 与当前已合入的 pytest 测试，job 名仍为 `skeleton (3.11)`、`skeleton (3.12)`；名称不代表只运行导入烟测。运行次数与结论以 [Actions](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions) 对应提交为准；CI 不代替生产性能、受控检出率或双 Linux 验收。`CODEOWNERS` 当前是注释占位，不能充当已启用的保护规则。
- 接口冲突由 A 会同 B1、B2、C4 审；按受影响领域邀 C2/C3/C5/C6，结论和兼容影响落在 #2 的评审记录或后续 ADR。只有评审并合并后才可称该版接口冻结。
- 依赖版本尚未锁定；Ubuntu/RHEL 全新克隆均尚未完成；脚本执行位已由 A 单独开 Issue 交 C6 修复，不挤进本文 PR。

1. 站会前核对自己当前分支、未提交的文件；三个终端相同：

   ```sh
   git branch --show-current
   git status --short --untracked-files=all
   ```

   ✅ 成功标志：能向团队准确报出当前 Issue、改动文件和阻塞；出现越权文件或教师资料立即停下，不用 `git add -A` 一股脑提交。

## 6. 风险登记与止损动作

| 风险 | 最早信号 | 负责人／止损 |
|---|---|---|
| 性能口径不清或百万点含写盘超时 | 只有算法耗时，没有落盘和环境记录 | B1 + C4 + C5：第 1 天提出脚本与计时口径；小组口径已确认，正式结果双 Linux 留证 |
| Schema/CLI/配置键未评审先各自实现 | 不同 PR 用不同字段、退出码 | A + C3 + C4：先记录接口评审和兼容测试，冻结后才扩大并行 |
| 真值泄漏造成虚假 100% | consumer/analysis 接触 `private_truth` 或故障事件位置 | B2 + C2 + C5：仅独立验证器读真值；报告同时披露正常误报与检测延迟 |
| 事务/告警状态恢复不完整 | `kill -9` 后漏窗、重警或只靠内存记状态 | B2 + C2：先审跨分片状态方案，再做同事务重放测试 |
| 跨平台与依赖失败 | RHEL 缺目标 Python、未锁依赖、脚本 `100644` | C6 + A：第 1 天建两台 VM；记录故障；脚本先用 `bash`，执行位修复由 C6 在单独 Issue 处理 |
| 信息误入公开仓库 | 新文件含教师材料、密钥、个人手册、大型数据 | 全员：资料置于仓库外；提交前检查暂存区，问题立即停推送 |

## 7. 答辩准备：12 分钟陈述 + 8 分钟问答

**这是建议的讲述分配和素材清单，不代表指标已通过。**九人每人至少能说明自己的输入、输出、修改、测试和局限；A 控时，任一证据缺失时把结论表述为“待验证”。可把陈述分为：A 需求/风险/结构（约 2 分钟）；C4 + C3 协议/配置/生产入口（约 2 分钟）；B1 + C1 仿真与故障（约 2 分钟）；B2 + C2 特征/告警/存储与重放（约 2 分钟）；C5 独立验证与结果局限（约 2 分钟）；C6 双 Linux 操作与故障处理（约 2 分钟）。问答 8 分钟由 A 主持，具体模块负责人答接口、性能、误报、恢复、两系统差异；未参与验证的成员不代答数据。

| 素材 | 提供者 | 必须能核对 |
|---|---|---|
| ≥8 用户故事、架构与评审记录、≥3 风险/里程碑 | A（#2） | 文档版本与决定依据，不能称评审前接口已冻结 |
| NPZ/Manifest、SHA 拒收、配置合法/非法 | C4（#3/#11）、C3（#4） | 分片字段、校验与未来实际 CLI 退出码 |
| 百万点生成+落盘、同 seed、spike/step/burst | B1（#5）、C1（#6） | 硬件/系统/seed/计时边界、可核验真值而非截图猜测 |
| 特征/评分/误报、SQLite 幂等与强杀恢复 | B2（#7）、C2（#8） | 窗口结果、事务与恢复前后行数 |
| ≥15 单测、≥20 用例、≥3 独立故障注入、检出率/误报 | C5（#9）汇总各负责人 | 测试实际输出、summary JSON/MD、测试集和检测延迟口径 |
| Ubuntu 与 RHEL 的新克隆、安装与演示 | C6（#10） | 每台独立命令、Python 版本、退出码与结果文件 |

1. 演练前只检查当前仓库跟踪的素材，实际证据尚未生成就标“待完成”：

   ```sh
   git ls-files 'docs/requirements.md' 'docs/architecture.md' 'docs/test_plan.md' 'docs/test_report.md' 'docs/deployment.md' 'docs/operations.md' 'docs/presentation.md'
   ```

   ✅ 成功标志：分清“文件存在”与“内容完整/实测已通过”；A 收齐各人真实证据后才能把 M4 标成完成，并进行 12+8 分钟演练。
