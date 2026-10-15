# G15 预测性维护场景沙盘：开发协作与质量规范 V1.0

> 状态：待全员确认的团队规范。**必须先阅读《G15_架构设计与接口契约_v1.0.md》**，本规范不得与评审后冻结的公共接口冲突。D1–D14 是相对排期模板，规范的目标是降低协作摩擦并让现场验收可复现。
> 2026-10-15 更新：D1–D14 是相对排期模板，实际截止日待确认；先读资料包根目录 `00_先读_统一决策与待确认事项.md`。当前接口、代码和测试尚未完成或冻结。

## 1. 九人责任、代码所有权、代理工具

| 人员 | 主责 | 代码/文档归属 | 交付验收标准 | AI 工具 |
|---|---|---|---|---|
| A 组长 | 项目经理、架构、集成、代码合并、双系统最终验收 | `docs/feasibility.md`、`docs/requirements.md`、`docs/architecture.md`、`docs/adr/`、`.github/` | 文档可追溯、CI 绿、端到端全通过 | Codex、WorkBuddy、GLM |
| B1 | 仿真器技术负责人、算法与性能 | `simulation/engine.py`、`degradation.py`、`noise.py` | 同 seed 重现、100 万点 <60s（含写盘） | DeepSeek Harness（先核对具体模型） |
| C1 | 故障注入 | `simulation/faults.py`、故障用例 | 可指定位置/幅度/时长；真值正确 | GLM 5.3 |
| C3 | 配置、校验与薄 CLI | `config.py`、`configs/`、`cli.py`、`__main__.py` | YAML 调参无需改 Python；CLI 只解析参数、分派命令、映射错误码 | GLM 5.3 |
| C4 | 数据接口与生产入口 | `pipeline/schema.py`、`serialization.py`、`producer.py`、`docs/interface.md` | Manifest/NPZ 契约稳定、原子落盘、错误输入被拒收 | GLM 5.3 |
| B2 | 下游算法技术负责人 | `analysis/features.py`、`health.py`、`alarms.py` | 函数有测试，健康评分 0–100，受控故障检出 | GLM 5.3 |
| C2 | 消费、数据库与恢复 | `pipeline/consumer.py`、`storage/` | 重复消费幂等，kill 重启可恢复 | GLM 5.3 |
| C5 | 独立验证、报告与测试汇总 | `tests/integration/`、`tests/fault_injection/`、`reporting/`、`docs/test_plan.md`、`docs/test_report.md` | 汇总全员至少 15 单测、20 用例；独立匹配真值、报告误报与性能证据 | GLM 5.3 |
| C6 | 环境、部署、运维、交付文档 | `scripts/`、`docs/deployment.md`、`docs/operations.md`、README 协助 | Ubuntu/RHEL 实际克隆部署、运行手册 | GLM 5.3 |

**两条指导链**：B1 负责指导 C1/C3 的仿真与配置配合；B2 负责指导 C2 的数据分析/存储配合；A 与 C4 审核公共数据接口，并协调 C3 的 CLI；A 审核 C5 的验收口径和 C6 的双 Linux 证据。每位实现者写并运行本模块测试，C5 负责独立集成验证和全组证据汇总。代码所有权表示首要责任，不意味着其他成员可以绕开 PR 审查直接修改。九人都必须有真实有意义的提交、可讲述的输入输出与测试。

## 2. GitHub 仓库与分支工作流

### 2.1 主分支保护
- `main` 只允许通过 PR 合并，禁止直接 push、强推和删除。
- PR 至少 1 人 Review，通过 `pytest`/Black/Pylint 与安装烟测；公共契约、依赖、CI 文件由 A 复审。
- 开发采用短分支：`feat/<issue>-<slug>`、`fix/<issue>-<slug>`、`test/<issue>-<slug>`、`docs/<issue>-<slug>`。
- 尽量保持 PR 修改范围在单一模块、单一 Issue；不把“重构全项目”混进普通功能 PR。
- 每日同步主分支，合并前检查 CI 和冲突；禁止提交 `.venv/`、`runs/`、缓存、Token、1M 原始数据。

### 2.2 Commit message
推荐 Conventional Commits：`feat(sim): add seeded degradation`、`fix(consumer): handle invalid manifest`、`test(alarm): add spike case`、`docs(ops): add rhel deployment`；允许中文说明，但前缀/模块要规范。禁止“修改”“测试一下”“AI 写的代码”等不可追溯消息；小步真实提交，不刷 commit 次数。

### 2.3 Issue 模板（复制使用）

```md
## 背景与目标
## 负责人 / 审核人 / 优先级 / 截止时间
## 修改范围（允许修改的文件）
## 输入契约 / 输出契约 / 依赖
## 实施步骤（最多 3–5 项）
## 验收条件（给出明确命令和期望结果）
## 风险与阻塞
```

### 2.4 Pull Request 模板（复制使用）

```md
## 关联 Issue
Closes #...
## 改动内容（做了什么、为什么）
## 修改文件与公共接口变化
- [ ] 未改公共接口 / [ ] 已附 ADR 和兼容测试
## 本地测试证据
命令：python -m pytest -q
结果：...
## 自检
- [ ] Black/Pylint 合格
- [ ] 新增/修改测试
- [ ] 无账号、Token、绝对路径、大数据
- [ ] README/接口/配置同步更新（如适用）
## 风险与回退方式
```

### 2.5 Definition of Ready / Definition of Done

**进入开发（DoR）**：有 Issue、负责人、明确输入输出、文件边界、依赖条件和一条可执行验收命令。

**完成（DoD）**：有代码或实物文档、有测试、静态检查合格、日志/错误已处理、接口没擅改、PR 至少 1 人批准、CI 全绿、可在主分支重新验证、贡献记录完整。**AI 说通过 ≠ 测试通过；功能截图 ≠ 项目完成。**

## 3. Python 编码规范

1. **基础风格**：PEP 8；统一 UTF-8、4 空格、LF 行尾；统一 Black 行宽 88；`snake_case` 用于变量/函数/模块，`PascalCase` 用于类，`UPPER_SNAKE_CASE` 用于常量。
2. **类型注解**：公开函数必须有参数和返回值类型；跨模块数据优先 `@dataclass(frozen=True)` 或明确的类型定义；不任意传层层嵌套的裸 dict。
3. **函数职责**：单个函数完成一个可测试目标；业务公式与 IO 分离；CLI 不含算法；算法函数不读文件、不连接 SQLite、不 `print`。
4. **文档字符串**：公开函数写作用、输入单位/范围、返回值、可能异常、随机性/副作用；复杂公式在文档和代码注明口径。
5. **异常**：禁止裸 `except:`；只捕获能处理的异常并保留根因（`raise ... from exc`）；配置或输入校验不能用 `assert`（可能被优化移除）；用户给出的错误配置返回明确异常与非零退出码。
6. **日志**：模块使用 `logging.getLogger(__name__)`；业务核心不 `print`；`print` 只用于用户可见 CLI 摘要。日志里包括 UTC、run_id、module、event、error_code；敏感信息不得入日志。
7. **配置与路径**：业务数字和规则从 YAML 读取；通过 `pathlib.Path` 处理路径；不能使用 `/home/<某人>/...` 或只支持 Windows 的路径；禁止运行时静默下载模型和数据。
8. **资源**：文件/数据库连接使用 context manager；确保线程/子进程能退出；不得在 import 时执行昂贵操作；不能缓存无限增长的数组或全局字典。
9. **数值安全**：时间统一 UTC；样本序号 int64；浮点有限性与除零检查；统一单位；健康评分固定 0–100；不能混用超额峭度和普通峭度定义。
10. **确定性**：不可使用全局 `np.random.seed` 污染其他模块；每次 run 有明示 seed、代码和配置版本；测试不依赖操作系统当前时间。

### 3.1 推荐代码写法

```python
from dataclasses import dataclass
import numpy as np
from numpy.typing import NDArray

@dataclass(frozen=True)
class SignalFeatures:
    rms: float
    std: float
    peak_to_peak: float


def extract_basic_features(signal: NDArray[np.float64]) -> SignalFeatures:
    """提取基本振动特征；signal 单位 g，要求非空且数值有限。"""
    if signal.ndim != 1 or signal.size == 0:
        raise ValueError("signal must be a non-empty 1D array")
    if not np.isfinite(signal).all():
        raise ValueError("signal contains NaN or Inf")
    return SignalFeatures(
        rms=float(np.sqrt(np.mean(np.square(signal)))),
        std=float(np.std(signal)),
        peak_to_peak=float(np.ptp(signal)),
    )
```

这是**风格示例**，不等同于整个特征引擎已经实现。

## 4. 工具和依赖门禁

- 本地必跑：`python -m black --check src tests`、`python -m pylint src/g15 --fail-under=8.5`、`python -m pytest -q`；在 CI 使用相同命令。
- `pyproject.toml` 管包元数据和 Black/Pytest/Pylint 配置；`requirements.txt` 与 `requirements-dev.txt` 使用经验证的**确切版本**，版本升级走 PR。
- README 写 Python 版本范围、Linux 支持、安装命令、运行命令、输入/输出示例、已知限制。不能假定使用者已创建课程 `pp03` 环境。
- CI 至少覆盖 Python 3.11/3.12 的单测矩阵；Ubuntu 与 RHEL 的真实运行由部署记录证明。若 GitHub Actions 无现成 RHEL Runner，可在本地 RHEL VM 验证并上传命令日志。
- 性能门禁由固定机器或 VM 作为基准，不因共享 CI Runner 偶发抖动而伪判失败；报告硬件和输入规模。

## 5. 测试规范与用例分层

### 5.1 层次

| 层次 | 范围 | 门禁 |
|---|---|---|
| 单元测试 | 配置校验、仿真、故障、特征、评分、告警、序列化 | ≥15 条，全部通过 |
| 集成测试 | 完整 `simulate → consume → report` | 正常与故障场景通过 |
| 契约测试 | 字段、类型、单位、顺序、分片边界、SHA、版本 | 不兼容时明确失败 |
| 故障注入 | 业务故障、坏数据、`kill -9` 重放 | ≥3 个独立场景，有日志和结果 |
| 性能测试 | 1M 点输出并落盘；内存、耗时、速率 | 60 秒硬指标 |
| 跨环境测试 | Ubuntu 与 RHEL clean install + demo + pytest | 两系统分别留证据 |
| 回归测试 | 修 Bug 前先补复现测试，再提交修复 | 修复后原问题不复发 |

### 5.2 至少 20 个可执行用例（初始矩阵）

| ID | 场景 | 期望 |
|---|---|---|
| T01 | 默认 YAML | 加载成功 |
| T02 | 缺失 YAML | 退出码非零、错误信息明确 |
| T03 | 负噪声标准差 | 被配置校验拒绝 |
| T04 | 故障概率 >1 | 被拒绝 |
| T05 | 未知故障类型 | 被拒绝 |
| T06 | 同 seed 两次运行 | 原始数值相等 |
| T07 | 更改 seed | 信号噪声变化 |
| T08 | 正常场景 | 点数、单位、时间戳合法 |
| T09 | 慢退化场景 | 趋势方向正确 |
| T10 | spike 故障 | 注入区间与真值一致 |
| T11 | step 故障 | 注入起点、持续行为一致 |
| T12 | burst 故障 | 注入持续窗口正确 |
| T13 | 百万点仿真 | 总点数=1,000,000 且 <60s |
| T14 | NPZ 缺少字段 | schema 失败并日志可查 |
| T15 | 分片校验和错误 | 拒绝数据/不写假结果 |
| T16 | 不完整尾窗 | 按配置 drop/error，统计正确 |
| T17 | 常值振动窗口 | RMS 正确、峭度安全处理 |
| T18 | 健康评分 | 始终在 [0,100] |
| T19 | 告警阈值和连续窗口 | 黄/红触发符合规则 |
| T20 | 注入故障评估 | 匹配率 100%（受控预设集），误报有统计 |
| T21 | 重复消费同一运行 | 结果条数不增加 |
| T22 | 处理时 kill -9 后重启 | 未提交可重放；最终无重复 |
| T23 | 配置只改速率 | 无源代码修改且结果变化 |
| T24 | Ubuntu 全新克隆 | 安装、demo、验证成功 |
| T25 | RHEL 全新克隆 | 安装、demo、验证成功 |

`pytest` 的测试函数不应假造成功数据或依赖相同算法复制的预期结果；重要公式用手算参考值和边界输入交叉验证。测试要明确耗时/环境基准，区分单元测试数量与测试用例数量。

### 5.3 故障注入和恢复策略

**业务故障**：程序从原始数据计算特征并产生报警，验证器单独用真值匹配；不得让下游直接读取注入位置。**坏文件**：改动已生成的一个分片字节，读取应产生 SHA 错误，不得写入部分可信结果。**进程崩溃**：在未消费完时 kill，重启读取 `processed_chunks` 并跳过已经提交的分片；所有写入和游标必须同一事务，校验最终条数和告警数。再补配置不合法/磁盘无权限作为可选增强。

### 5.4 数据质量和统计口径

- 样本点数与分片行数一致；分片连续性通过 seq 验证。
- `processed_chunks` 主键保证同分片不会处理两次，feature/alarms 的唯一键保证重复重放不会产生重复记录。
- `detection_rate = detected_fault_events / total_fault_events`；报告事件数、检出数、未检数、误报数、允许的检测延迟窗口、使用的 seed、规则版本。
- 健康分不能通过硬编码事件位置或将所有窗口判故障的方式获得 100% 捕获；正常测试必须展示误报表现。
- 上述检测指标仅是教学仿真和受控数据场景验证，不是现场真实设备模型质量保证。

## 6. 部署、运维与安全规范

### 6.1 目标系统

- 用户确认 Linux 发行版无指定、虚拟机验证获认可；小组暂选 Ubuntu 24.04 LTS 与 Red Hat Enterprise Linux 9（RHEL 9），在两套不同发行版的虚拟机或系统中各自从 GitHub 公开仓库全新克隆安装/运行；使用有效的 Python 3.11/3.12 解释器，RHEL 9 需先确认该解释器是否已安装。
- 使用 `.venv`；shell 以仓库路径为工作目录，`set -euo pipefail`，命令失败返回非零，不允许脚本“看上去成功”。
- `./scripts/setup.sh`：建环境、装固定依赖、检查版本；`demo.sh`：配置验证→仿真→消费→报告；`verify.sh`：全部可执行验收；`benchmark.sh`：固定种子百万点基准；`clean.sh`：只清理明确的自动产物，不能随意删除源码/用户数据。
- 自动测试必须先在空 `runs` 目录执行，不假设本机预先生成过结果。
- 依赖安装可能要联网，但**运行时**不得依赖云端 AI 或下载数据。现场网络/PyPI 问题可备用 wheelhouse（取得教师许可后使用），不能用备用方案代替正式现场克隆演示。

### 6.2 运维手册要求

至少回答：启动停止命令、配置修改/重启、数据目录位置、怎样查看健康与告警结果、怎样查看日志、怎样判断是否成功、常见错误和恢复操作、备份/清理策略、不同 Linux 的安装差异、退出码意义。

### 6.3 安全与仓库规则

- 不将 `.env`、Token、密钥、个人数据、大型 NPZ、数据库文件、`.venv` 提交 GitHub。
- Python 读 NPZ 使用 `allow_pickle=False`；文件名和路径先校验，禁止从未受信任 manifest 拼任意路径跳出 run 目录。
- SQLite 只用参数化 SQL，绝不拼接输入字符串到 SQL。
- 文件写入先完成临时文件再重命名；不允许有不受控的 `rm -rf` 指令。
- 公开日志不能泄露账号、模型 API 密钥；课程样例仿真数据可以公开，但要确保不含真实厂区信息。

## 7. AI 编程协作规范

1. **AI 工具不拥有架构决策权**。公共 Schema、配置键、外部命令、依赖、数据库迁移、算法评分口径的改动一律提 Issue 并经 A 审核。
2. **提示词只发单个 Issue**，描述输入输出、修改文件、禁止触碰区域、测试命令、验收条件。
3. **生成代码先自读、后运行**；负责人能解释代码为什么正确、如何处理边界条件、如何定位报错。
4. **不向 AI 提交密钥或私人数据**；优先本地仿真与公开示例数据。
5. **禁止虚构测试结果**、自动删除失败测试、私自降低硬门槛、通过直接读取 truth 实现假检测。
6. 工具使用分工：A 可用 Codex 做跨模块 PR Review/修复，WorkBuddy 维护文档和计划，GLM 交叉评审；B1 使用 DeepSeek Harness 构建与评审仿真模块（应确认当前底层模型）；B2/C 层用 GLM 5.3 处理指定 Issue。

### AI Issue 提示词通用模板

```text
你正在参与 G15 项目，必须遵守 docs/interface.md 和项目代码规范。
Issue: <编号和目标>
只允许修改：<文件列表>；禁止修改：<文件列表>
现有输入契约：<类型、字段、单位、错误情况>
要求输出：<函数签名、文件/数据库格式>
必须实现的边界：<3-5条>
必须补充的测试：<pytest 测试名/场景>
本地运行命令：python -m pytest ...
不允许修改 schema、CLI、依赖、健康评分口径或测试阈值。
先说明修改方案和需要影响的文件，发现契约冲突先提问。
```

## 8. 软件工程交付链与 TOGAF 参照

| 阶段 | 产出 | 审查标准 |
|---|---|---|
| 可行性 | `feasibility.md` | 技术/进度/人员/成本/风险与应对 |
| 需求 | `requirements.md` | 8+ 用户故事、数值 NFR、需求追踪表 |
| 架构 | `architecture.md`、`interface.md`、`database.md`、ADR | 组件图、数据流、部署图、接口契约、方案比较 |
| 分工与计划 | Issue、看板、WBS、里程碑 | 任务到人、≥3 里程碑、≥3 风险 |
| 开发 | 源码、PR、单测、配置、依赖 | 功能可复验、Black/Pylint/pytest |
| 验证 | `test_plan.md`、`test_report.md`、性能证据 | ≥20 用例、≥3 故障注入、关键指标实测 |
| 部署运维 | `deployment.md`、`operations.md`、Shell 脚本 | 双 Linux、现场克隆、故障恢复 |
| 复盘答辩 | `retrospective.md`、`contributions.md`、`presentation.md` | ≥5 踩坑、成员可解释、演示闭环 |

参照国家《系统与软件工程 软件生存周期过程》的全生命周期思路组织证据；参照 TOGAF ADM 将业务需求、数据架构、应用组件和技术部署分层表达。**这是小组工程方法借鉴，不等于项目经过标准认证。** 标准的具体版本与适用条款应在正式答辩材料定稿前核验。

## 9. 两周执行门禁

| 时间 | 门禁 | 没通过的处理 |
|---|---|---|
| D1–D2 | GitHub、需求/ADR 草稿、接口冻结、最短 stub 链路 | 暂停扩展，A 决策冻结契约 |
| D3–D5 | 核心仿真、配置、消费、评分、告警可集成 | 全组修通接口，禁止新需求 |
| D6–D7 | 三场景通过、百万点第一次实测、功能冻结 | 优先补主链与性能，无关工作暂停 |
| D8–D10 | 15+ 单测、20+ 用例、3+ 故障注入、恢复和检出率证据 | 进入仅修缺陷模式 |
| D11–D12 | Ubuntu/RHEL 全新克隆通过、代码冻结 | 先解决依赖/路径/环境不一致 |
| D13–D14 | 两轮模拟答辩、Release、文档全套 | 仅处理阻塞交付的问题 |

**每日节奏**：15 分钟站会（昨天产出/今天 PR/阻塞）；固定集成时间；每个 PR 需 CI 绿；每次功能合并后主分支 demo 仍要能跑。A 不替大家做所有实现；每人对自己的模块、测试和提交记录负责。

## 10. 答辩与终验 Checklist

- [ ] README 首页回答：断电重启、并发容量、审计溯源、换机部署、生命周期/TOGAF 映射。
- [ ] G15 五项功能均有运行结果与测试对应。
- [ ] 100 万点仿真 <60 s，记录操作系统/CPU/Python/时间/生成和落盘路径。
- [ ] 预设受控故障 100% 匹配，同时报告误报及测试局限。
- [ ] YAML 改参数无源代码修改，结果可复现。
- [ ] 生产和消费独立 CLI，接口字段、类型、单位、版本有文档。
- [ ] ≥8 用户故事、≥3 里程碑、≥3 风险、至少 1 条 ADR。
- [ ] ≥15 单测、≥20 测试用例、≥3 故障注入，CI 全绿。
- [ ] `kill -9` 后可重放、数据不重复；坏分片有明确报错；结构化日志可查。
- [ ] GitHub 上公开可交付的版本/tag，九人有真实提交与 PR，贡献表一致。
- [ ] Ubuntu 24.04 + Red Hat Enterprise Linux 9（RHEL 9） 实际按 README 从空环境运行并留下记录。
- [ ] 运维手册、故障处理、性能报告、复盘（≥5 踩坑）、完整 12+8 分钟 L3 答辩脚本准备就绪。

## 11. 规范签署（建议在 kickoff 开会逐人确认）

| 项目 | 决议 |
|---|---|
| 规范版本 | V1.0 |
| 项目负责人 | A |
| 公共接口负责人 | A + C4 |
| 仿真子系统负责人 | B1 |
| 下游子系统负责人 | B2 |
| 测试负责人 | C5 |
| 部署负责人 | C6 |
| 规范生效条件 | 团队 kickoff 确认；GitHub 主分支合并为冻结版本 |
| 变更方式 | Issue → ADR（必要时）→ PR → 审核 → CI → 合并 |
