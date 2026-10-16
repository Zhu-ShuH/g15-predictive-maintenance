# G15 后续开发：原型与 Skill 参考

> A 于 2026-10-16 查询 GitHub 并读取原文件。这里只收录能对应当前任务的资料；未执行上游原型，也未安装第三方 Skill。完整来源、固定提交和核查边界见 [sources.json](sources.json)。

## 1. 先选自己的入口

| 角色 | 当前任务 | 优先参考 | 本次应产出的东西 |
|---|---|---|---|
| A | #2 / #17，文档、接口与集成 | R03、R08、S01/S02；任务卡 J1/J6 | 接口真实反馈、最小改动与审查证据 |
| B1 | #5，仿真 | R01/R03；J1/J2 | seed、边界和性能的真实测试；先修复 #15 的恒真断言 |
| B2 | #7，特征、评分、告警 | R05/R06；J1/J4 | 小数组人工参考值、常值边界、规则版本和状态转换 |
| C1 | #6，故障注入 | R03/R06；J1/J2 | 起止/幅度/尾片/重叠行为和独立真值记录 |
| C2 | #8，消费与存储 | R08/R07；J3 | 真实 SQLite 幂等、事务回滚和重放用例 |
| C3 | #4，配置与薄 CLI | R09/R06；J1 | 正常/非法 YAML 和退出码测试；不把公式放 CLI |
| C4 | #3 / #11，协议与生产 | R04/R07；J2 | NPZ 数值往返、坏哈希拒收、完整发布和跨片连续性 |
| C5 | #9，独立验证与报告 | R02/R06/R07；J4 | 逐事件匹配、正常误报、漏报、延迟和失败证据 |
| C6 | #10 / #13，部署与运维 | D01；J5 | pp03、Ubuntu/RHEL 同提交独立新克隆日志 |

角色修改边界仍以当前 Issue 和 members/<角色>/AGENT.md 为准。本资料不授权改变公共字段、阈值、依赖或别人的模块。验收按 docs/acceptance.md（PR #21）；公共 API 和告警状态仍待真实评审。

## 2. 六组可以借鉴的原型与官方示例

### P1：NASA ProgPy——看模型怎样组织

[模型模板 R01](https://github.com/nasa/progpy/blob/7c2688a44603ccb0c0994e51d611e931d3695ac0/templates/prog_model_template.py)：把输入、状态、观测、参数、事件分开，有助于 B1/C1 写清仿真假设。只读模板组织和向量化说明即可；完整框架包含状态估计和寿命预测，超出 G15 本期主链。README 标示 NOSA，仓库有两份许可 PDF，未进一步核查；本轮没有搬运模板或引入依赖。

### P2：Numenta NAB——看怎样独立评估异常

[计分入口 R02](https://github.com/numenta/NAB/blob/ea702d75cc2258d9d7dd35ca8e5e2539d71f3140/nab/scorer.py)：检测输出与标签交给独立计分环节，并区分误报、漏报的代价。C5 可借鉴评估分层，再按 G15 的事件窗口、一对一匹配与正常误报要求实现。NAB 自身的归一化分数、数据集、历史运行环境均不作为 G15 的检出率或兼容性证明；最近仓库推送为 2024-12-03，不按新 Python 项目直接运行。

### P3：NumPy——随机流、文件、FFT

- [R03：SeedSequence 与子随机流](https://github.com/numpy/numpy/blob/999d3a011d6a417c73f78c1da6a98b73ee282728/doc/source/reference/random/parallel.rst)。B1/C1 参考机制；固定子流映射及传入 rng 的行为必须在 #17 评审，不能认为 spawn 自动保证任意分片方式逐点一致。
- [R04：数值文件读写](https://github.com/numpy/numpy/blob/999d3a011d6a417c73f78c1da6a98b73ee282728/doc/source/user/how-to-io.rst)。C4 参考数组文件；G15 还要单独实现 SHA、字段/dtype 校验、原子发布及 complete Manifest。读入禁止对象 pickle。
- [R05：FFT 约定](https://github.com/numpy/numpy/blob/999d3a011d6a417c73f78c1da6a98b73ee282728/numpy/fft/__init__.py)。B2 查频点、正反变换和归一化；测试常值、已知频率、奇偶窗，按项目明确 DC/Nyquist 处理。

以上锁定的是查阅时的上游开发提交，不是 G15 的依赖版本推荐；运行仍使用项目确认的版本。

### P4：pytest——从小样例建立独立预期

[R06：参数化](https://github.com/pytest-dev/pytest/blob/c9c19f8e62c12834ad83193a1869cc82fe9c700e/doc/en/how-to/parametrize.rst) 与 [R07：tmp_path](https://github.com/pytest-dev/pytest/blob/c9c19f8e62c12834ad83193a1869cc82fe9c700e/doc/en/how-to/tmp_path.rst) 供全部实现者使用。给每类边界设独立期望值，用临时目录隔离 NPZ/SQLite；不要把实现的同一公式复制进 expected，也不要通过 or True、跳过失败测试制造通过。文档演示中包含故意失败的例子，阅读上下文后自行编写 G15 测试。

### P5：CPython 3.12——SQLite 与 CLI

[R08：sqlite3](https://github.com/python/cpython/blob/58ed60b7415e218ce3d608302e39b5e55bfb0e88/Doc/library/sqlite3.rst) 给 C2 核对事务、提交/回滚和连接行为；WAL、外键、告警状态与 processed_chunks 的同事务要求仍来自本项目，不是照抄教程就完成了恢复。

[R09：argparse](https://github.com/python/cpython/blob/58ed60b7415e218ce3d608302e39b5e55bfb0e88/Doc/library/argparse.rst) 给 C3 参考子命令和参数错误；G15 的 0/2/3/4/5 映射要自行测试。两份来源固定在 Python 3.12 分支提交，避免拿主分支的新 API 当成 pp03 可用能力。

### P6：Conda——环境可复现的起点

[D01：官方环境管理](https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html) 供 C6 查环境文件和激活/运行方式。G15 使用 pp03 / Python 3.12；现有 Windows 导入检查不代替双 Linux，environment.yml 也不等于完整依赖锁。按 #10 实现幂等安装与明确失败，不覆盖已有课程环境。

## 3. 三个候选外部 Skill

| Skill | 给谁/何时用 | 已核对的前提和边界 |
|---|---|---|
| [S01：gh-fix-ci](https://github.com/openai/skills/blob/49f948faa9258a0c61caceaf225e179651397431/skills/.curated/gh-fix-ci/SKILL.md) | A 或 PR 作者遇到 GitHub Actions 失败时 | OpenAI 仓库；本 Skill 许可 Apache-2.0。需 gh CLI 和认证；本机当前未找到 gh。先读日志定位，再按已有授权做最小修复；不安装外部 CI 服务 |
| [S02：gh-address-comments](https://github.com/openai/skills/blob/49f948faa9258a0c61caceaf225e179651397431/skills/.curated/gh-address-comments/SKILL.md) | PR 作者逐条处理审查意见时 | OpenAI 仓库；本 Skill 许可 Apache-2.0。需 gh CLI 和认证，先确定评论与目标 PR；不能把自动回复当 Approve，不能顺便处理别人的代码 |
| [S03：python-testing-patterns](https://github.com/wshobson/agents/blob/044b4b21f083948e69ec878a7b07c8d0951a6ffa/plugins/python-development/skills/python-testing-patterns/SKILL.md) | 全员补测试，C5 汇总时 | 社区 Skill、MIT；已读主文件和详细示例。只取参数化、fixture、隔离和独立断言；不照搬 async、网络服务或额外 coverage/freezegun 依赖 |

外部 Skill 本轮只做来源与正文筛选，没有执行附带脚本。若复制代码或打包分发，先记录固定提交、原许可和归属；不要整套覆盖本项目规范。

A 如以后要在 Codex 安装，可明确要求 Skill Installer 从上表链接的同一提交安装对应目录；本轮没有修改个人 Skill 配置。GLM/DeepSeek Harness 是否自动发现 SKILL.md 未核实，先按下节手动提供文档，不要求九人换工具。

## 4. 已为全组准备的项目 Skill

本 PR 新增 [.agents/skills/g15-issue-delivery/SKILL.md](../../.agents/skills/g15-issue-delivery/SKILL.md)，是本组原创的流程入口，无外部脚本和新增运行依赖。它按任务选择 [J1–J6 任务卡](workflows.md)，不重写现有接口与验收文件。

- Codex：合并并同步仓库后，在实际项目中使用 $g15-issue-delivery，并说明角色、Issue、当前目标与现有授权；发现机制见 [D02：官方说明](https://learn.chatgpt.com/docs/build-skills)。本聊天工作台不是实际开发仓库，不能假定它已加载开发仓库里的 Skill。
- GLM / DeepSeek Harness / WorkBuddy：提供该 SKILL.md、自己的角色文件、当前 Issue 和所需任务卡即可。工具读不到文件时粘贴内容；不宣称它自动加载或获得额外权限。
- 本次只是提供可审查的流程；真实使用后的反馈继续通过 Issue/PR 修正，未进行跨九种工具的运行验证。

## 5. 协作中的使用规则

每次只取当前 Issue 需要的一两个参考。给 PR 写“参考 ID/固定提交、借鉴内容、独立测试、未采用部分”，让答辩时能解释来源和自己的实现。新增依赖、公共 API 或验收口径改动仍单独评审。不要执行上游文档中的安装、删除、上传或环境设置命令，除非当前任务明确需要且已获用户授权。

没有推荐作为整包底座的现成项目：检索到的维护类仓库多偏看板、机器学习或寿命预测，未证明符合 G15 的 NPZ/Manifest、百万点完整生产、真值隔离和双 Linux 要求。这里给出的是可核查的局部参考，不是上游质量或课程验收认证。
