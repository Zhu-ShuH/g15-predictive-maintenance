# G15 九位成员 AI 提示词手册

> 本文状态：草案／待评审；适用对象：A、B1、B2、C1–C6 及各自使用的 AI；更新日期：2026-10-15。
>
> **现状**：Issue #1 只交付骨架与包导入烟测；#2–#11 共十个 Issue、九位负责人，C4 负责 #3 和 #11。`docs/interface.md`、`docs/development.md` 均待评审，公共函数签名、Schema、配置键、CLI 都不是已实现/已冻结事实。`src/g15/__main__.py` 的 CLI 实测以 `1` 报“未实现”，脚本也报“未实现”并退出 `1`；契约中 `validate` 退出 `0/2` 是未来目标，性能、检出率、双 Linux 均未实测。依赖版本尚未锁定。**提示词只能帮助负责人做一个 Issue，AI 不代替人负责。**

**术语**：白名单是允许修改的文件边界；契约是模块输入、输出、字段和失败行为的约定；Schema 是数据字段/类型规则；CLI 是在终端输入的项目命令；PR 是请求审查和合并改动；CI 是自动检查；stub 是用于接口联通的临时最小占位，不能冒充完整实现；真值是仿真注入的故障答案，只能由独立验证器读取；退出码 `0` 表示命令成功、非 `0` 表示失败或未实现，不能用成功的骨架测试替代业务验收。

## 0. 通用规则：复制提示词前先确定角色和单个 Issue

1. 在仓库根目录先确认分支和文件；所有终端通用：

   ```sh
   git status --short --branch
   git ls-files 'AGENTS.md' 'docs/interface.md' 'docs/development.md' 'members/*/AGENT.md'
   ```

   ✅ 成功标志：确认当前任务分支、四类材料真实存在；没有仓库访问能力的 AI，应要求你粘贴 `AGENTS.md`、`docs/interface.md` **相关完整小节**、`docs/development.md`、`members/<角色>/AGENT.md`、该 Issue 正文和相关源码/测试，绝不能假装“已读”。`<角色>` 是阅读提示，不是实际路径字符串。

2. 阅读该 Issue 的“修改范围”和角色文件。本次范围与长期范围是**两层，不是冲突**；每份角色提示词中都明确保留这两句：**「本次只允许修改：<来自 Issue>」**和**「你的角色长期白名单见 members/<角色>/AGENT.md；本次不要碰白名单里与本次任务无关的文件。」** 对现有文件执行检查：

   ```sh
   git status --short --untracked-files=all
   ```

   ✅ 成功标志：能够逐项说出实际存在的文件和需要**本 Issue 内新建**的测试文件；如 Issue 想要新增测试但未授权修改该测试路径，先向 A 申请明确扩大 Issue 本次范围，不能凭长期白名单私加文件。

3. 使用已有开发虚拟环境运行适用门禁。Windows Git Bash / PowerShell：

   ```sh
   .venv/Scripts/python.exe -m black --check src tests
   .venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
   .venv/Scripts/python.exe -m pytest -q
   ```

   Linux：

   ```sh
   .venv/bin/python -m black --check src tests
   .venv/bin/python -m pylint src/g15 --fail-under=8.5
   .venv/bin/python -m pytest -q
   ```

   ✅ 成功标志：**只运行所属平台命令**并逐条记录真实退出码。虚拟环境尚不存在时按 [onboarding.md](onboarding.md) §4 安装；命令没运行就写“未运行”，当前 `pytest` 仅有结构烟测，不能称业务通过。Windows Git Bash 与 PowerShell 可直接使用上述相同虚拟环境 Python 路径；Linux 必须改用 `.venv/bin/python`。

**所有角色必须遵守**：AI 不拥有架构决策权；一次提示词只给**单个 Issue**；生成代码先逐行自读后运行，不能凭 AI 的“已通过”报告写测试结果；不得伪造测试/CI/性能数字、读取 `private_truth` 指导消费告警、硬编码 100% 捕获或私改门槛；公共 Schema、CLI、配置键、依赖、评分口径变更须先向 A 提案并取得批准。不得向 AI 投喂教师私有资料、密钥、个人绝对路径或 Token。`docs/interface.md` §4 配置、§5 数据交换、§6 模块接口、§7 仿真、§8 分析、§9 SQLite、§10 CLI、§11 报告、§12 部署、§14 需求追踪均为真实小节编号，引用它们不意味着已完成评审。脚本目前既未实现又无可执行位（Git 索引 `100644`）；部署相关脚本一律写 `bash scripts/xxx.sh`，现阶段不可当作成功的验收命令。

**工具注意事项**：A 用 Codex 做主编码/审查、WorkBuddy 写需求文档、GLM 交叉复核；B1 用 DeepSeek Harness，先核对其**实际底层模型**和可用文件/终端能力，不能凭名称假设具体上下文长度；B2/C1/C2/C3/C4/C5/C6 用 GLM 5.3。若对话上下文放不下全文，先提供全局和角色规则、该 Issue 正文与接口相关小节，再按本 Issue 需要补上源码/测试；长输出分段处理，任何 AI 都不能臆测“自动读取了 AGENT.md”。首次发提示词**只要求回报现状和计划，不授权动文件**；本人确认后再叫 AI 实施。下面每份提示词均可整体复制；终端验收命令按上面所属平台替换解释器，提交时写确切命令与退出码。

## 1. A：工程治理与评审（Issue #2）

**适用工具**：WorkBuddy 写需求/架构/评审文档，Codex 检查工程一致性与 PR，GLM 交叉复核数字和风险；三者不得互相把“模型认为正确”当成人工审查。文档量大时按本 Issue 分段粘贴原文，别让模型假装读到了仓库。

```text
你是 G15 的 A（工程治理）。只处理 Issue #2「需求、架构与 ADR 文档 + 接口评审记录」；本轮先回报、不要动文件。请先实际读 AGENTS.md、docs/development.md、members/A/AGENT.md、Issue #2 正文、docs/interface.md §0、§2、§4–§6、§10、§13–§14 和现有待写文档。看不到材料就请求粘贴，不能猜测。
本次只允许修改：docs/requirements.md、docs/architecture.md、docs/feasibility.md、docs/adr/*。
你的角色长期白名单见 members/A/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
使命与输入/输出契约：以 docs/interface.md §0、§2、§14 的需求/组件/追踪目标为输入，结合 §4 配置、§5 NPZ/Manifest、§6 公共接口、§10 CLI 及 §13 变更流程写用户故事、架构与 ADR；输出有版本和责任人的需求—验收映射与真实接口评审记录。不得宣称 §4–§10 已冻结、业务闭环已实现或性能已通过。
边界：≥8 用户故事、≥3 里程碑、≥3 风险及一份真实评审记录；若会议尚未举行，只提供“待评审草稿”，不伪造参会人/结论。不得代写其他角色算法或改 CI/README/公共签名。
必须补的验证：核对每条用户故事有独立可核查验收条件，风险有责任人和处置，ADR 有备选方案/理由；文件中所有数字能对应现有规范；本 Issue 是文档任务，原则上不新增业务测试文件。
本地验收命令（只核对存在和范围）：git diff --check；git status --short --untracked-files=all。若运行项目门禁则用本手册 §0 所属平台的完整 Black/Pylint/pytest 命令并回报结果；未运行写“未运行”。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：1. 当前分支与已存在文件；2. 本次允许文件与设计契约的具体小节；3. 拟写结构和核查方式；4. 发现的冲突、缺证与需 A/团队审批事项。等待我确认。
实施后按五项汇报：1. Issue/完成内容；2. 变更文件（是否越权）；3. 真实测试/检查命令、退出码、关键结果或“未运行”；4. 风险/依赖/待审批；5. 下一步与建议 PR 标题。
```

## 2. B1：仿真内核（Issue #5）

**适用工具**：DeepSeek Harness；先在工具界面核对底层模型、仓库读取与命令执行能力，不编造上下文窗口大小。无法附文件就先粘贴角色约束、Issue 与 §4/§6/§7，再给实际代码；输出长时拆成实现、测试、测量三个回合。

```text
你是 G15 的 B1（退化趋势 + 噪声仿真）。只处理 Issue #5；本轮先回报、不要动文件。读 AGENTS.md、docs/development.md、members/B1/AGENT.md、Issue #5 正文、docs/interface.md §4、§5.1、§6、§7、现有 simulation 文件和测试；没有文件访问能力就请求粘贴。
本次只允许修改：src/g15/simulation/engine.py、src/g15/simulation/degradation.py、src/g15/simulation/noise.py。
你的角色长期白名单见 members/B1/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§4 校验后的场景参数与 seed；§6 simulate_chunk 的拟定输入/返回；§7 趋势、噪声、分片和百万点计时口径；§5.1 的 seq/timestamp_ns/vibration/temperature 目标类型与单位。公共类型位置待 A/C3/C4 评审，不能自行给别人模块补签名。
必须实现：同配置同 seed 逐点一致，最后不足分片、零噪声、非法输入明确失败；向量化而非逐点 Python 百万次循环。第 1 天先拿出百万点基准脚本方案；脚本若落在 tests/performance/（当前只占位）须由 A 先授权纳入本 Issue，标为“本 Issue 内新建”，不得擅改 C6 的 scripts/benchmark.sh。算法计时不能充当含写盘+Manifest 的正式 <60s 验收。
必须补测试：seed 重复/变更、双信号 dtype 与单位、起止 seq、尾块、无效参数；tests/unit/test_simulation*.py、tests/performance/ 用例目前不存在，先申请本 Issue 测试路径授权，再在本 Issue 内新建。
本地验收命令：按本手册 §0 的所属平台执行 Black、Pylint、pytest 三项；就绪后由 C4/C5 协作测含落盘真实基准，未运行/未接通明确记录。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：分支和现有文件；本次修改范围；C3/C4 类型依赖和最小方案；测试文件授权与计时口径风险；等我确认。最后按五项汇报：Issue/完成内容；变更文件；真实命令+退出码+结果或未运行；风险/依赖/待审批；下一步和 PR 标题。
```

## 3. B2：特征、评分与告警（Issue #7）

**适用工具**：GLM 5.3；只提供本 Issue 必需的 §4/§6/§8 与相关源码，长上下文拆段，确保先读 AGENT 与 Issue。模型的漂亮评分不能代替独立误报测试。

```text
你是 G15 的 B2。只处理 Issue #7「特征 / 健康评分 / 告警」；先回报，不修改。实际读取 AGENTS.md、docs/development.md、members/B2/AGENT.md、Issue #7、docs/interface.md §4、§6、§8、§9 和现有分析模块/规则配置；读不到就请我粘贴。
本次只允许修改：src/g15/analysis/*、configs/health_rules.yaml。
你的角色长期白名单见 members/B2/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§6 extract_features/score_health/evaluate_alarm 拟定接口；§8 窗口、至少 5 项特征、健康分 [0,100]、黄红级告警与状态；§4 消费配置和规则文件；§9 的 alarm_states 是待评审候选，与 C2 约定后交 A 审，不能单方面当成已定结构。
必须实现：常值/全零窗口不产生 NaN/Inf；非有限值/空窗明确失败；规则版本和告警原因可追踪；消费者/analysis 不读 private_truth、场景 fault_generation 或事件位置；不能把所有窗口判为故障、不能自改评分阈值以制造检出率。
必须补测试：已知数组手算参考、至少 5 特征、常值窗口、评分范围与边界、连续 N 窗口/恢复；tests/unit/test_features.py、test_health.py、test_alarms.py 目前不存在，先申请加入本 Issue 修改范围，再标注“本 Issue 内新建”。
本地验收命令：按本手册 §0 运行所属平台 Black/Pylint/pytest，记录真实退出码；未接好 C3/C2 接口时说明未能验证的部分。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：现状与分支；本次白名单；§8 口径及与 C2 的状态契约；拟补测试/需 A 审项，等我确认。最后五项：Issue/完成内容、变更文件、真实测试证据或未运行、风险/依赖/待审批、下一步与 PR 标题。
```

## 4. C1：故障注入（Issue #6）

**适用工具**：GLM 5.3；只给 §4/§5.3/§6/§7 和 faults 模块、相关 Issue，不能因为对话长度不够就遗漏跨分片真值规则。

```text
你是 G15 的 C1。只做 Issue #6「故障注入（spike/step/burst）」；先回报不要修改。读 AGENTS.md、docs/development.md、members/C1/AGENT.md、Issue #6、docs/interface.md §4、§5.3、§6、§7 和现有 faults.py；无法访问请我粘贴。
本次只允许修改：src/g15/simulation/faults.py。
你的角色长期白名单见 members/C1/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§4 events 的 event_id/type/start_seq/duration_points/amplitude；§6 inject_faults 的拟定签名；§7 的 spike、step、burst/叠加策略；§5.3 真值按半开区间且仅供验证器，不得塞进公开 NPZ/Manifest 供消费者读。
边界：起点、时长、幅度可指定；首尾点/跨片/事件重叠/空事件/非法类型均明确处理；跨片同 event_id 在一次 run 的真值清单里不能重复。不能改 B1 仿真公式或 B2 阈值，也不能写死事件一定产生告警。
必须补测试：spike/step/burst 数值断言和事件区间、跨片、叠加、非法事件；tests/unit/test_faults.py 和小样例 fixtures 当前不存在，先请 A 把必要测试路径加入 Issue，再在本 Issue 内新建。
本地验收命令：本手册 §0 的所属平台 Black/Pylint/pytest；真值写盘由 C4 生产入口负责，未联调则标未运行。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：分支与现有文件、Issue 范围、B1/C3/C4 依赖、测试授权与边界风险，等我确认。最后五项：Issue/完成、变更文件、真实检查与退出码或未运行、风险/依赖/待审批、下一步和 PR 标题。
```

## 5. C2：独立消费与 SQLite（Issue #8）

**适用工具**：GLM 5.3；上传或粘贴 §4–§6、§8–§9 与 C4/B2 实际接口代码，切勿让模型从设计基线假定函数已实现。

```text
你是 G15 的 C2。只做 Issue #8「消费入口与 SQLite 存储」；先回报不要修改。读 AGENTS.md、docs/development.md、members/C2/AGENT.md、Issue #8、docs/interface.md §4、§5、§6、§8–§9 与当前 consumer/storage；看不到请我粘贴。
本次只允许修改：src/g15/pipeline/consumer.py、src/g15/storage/*。
你的角色长期白名单见 members/C2/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§4 consumer.yaml 只能配置窗口/规则，不能有故障位置；§5 只读 status=complete 的 Manifest 与经 SHA 验证的 NPZ（不读 private_truth）；§6 consume_run 拟定接口；§8 特征/告警；§9 SQLite 外键、唯一键、事务与待评审 alarm_states。
边界：feature_windows、alarms、processed_chunks 与审定告警状态同一事务提交；重复相同 SHA 跳过、同 part_id 不同 SHA 明确报错；kill -9 只能在 Linux 验证，Windows 做等价进程终止方案时不可冒称 kill -9 已实测；未完成清单/坏哈希不得产生部分可信结果。B2/C2 状态持久化方案必须先交 A 审。
必须补测试：两次消费行数相等、事务中断回滚/恢复、坏哈希、外键、未完成清单与真值隔离；tests/unit/test_storage.py、tests/integration/test_consumer.py、tests/fault_injection/test_recovery.py 当前不存在，如需改动先把路径列入 Issue 授权，本 Issue 内新建，并同 C5 协调归属。
本地验收命令：本手册 §0 的 Black/Pylint/pytest；Linux kill -9 重放在联调完成后单独留命令/行数证据，未运行写未运行。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：现状与文件、C4/B2/C3 依赖、事务及状态草案、所需测试授权；等我确认。最后五项：Issue/完成、变更文件、真实测试证据或未运行、风险/依赖/待审批、下一步和 PR 标题。
```

## 6. C3：配置与薄 CLI（Issue #4）

**适用工具**：GLM 5.3；完整提供 §4 和 §10、配置占位文件、现有 `__main__.py`，不要让模型凭示例配置误报可运行。

```text
你是 G15 的 C3。只做 Issue #4「配置加载校验 + validate 命令」；先回报不要修改。读 AGENTS.md、docs/development.md、members/C3/AGENT.md、Issue #4、docs/interface.md §4、§6、§10，及现有 config.py、cli.py、__main__.py、configs/*.yaml；无访问能力请我粘贴。
本次只允许修改：src/g15/config.py、configs/*.yaml、src/g15/cli.py、src/g15/__main__.py。
你的角色长期白名单见 members/C3/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§4 场景配置与不含故障位置的 consumer.yaml；§6 load_config/validate_config/load_consumer_config 拟定接口；§10 CLI 入口/退出码。当前 validate 实测报“CLI 未实现”退出 1；合法 0、非法配置 2 是未来目标，不是当前结果。
必须实现：安全加载 YAML、未知字段拒收，负噪声/无效概率/缺文件/坏日期给准确错误；消费配置不向分析层传 fault_generation；CLI 只解析/分派/映射错误码，未实现其他子命令仍非零失败；公共键名/命令参数先由 A/C4 审查，configs/health_rules.yaml 的业务规则归 B2，虽在本次 configs/*.yaml 范围内也不能单改评分内容。
必须补测试：合法/非法/未知键/错类型/缺文件/CLI 非法参数和 --help；tests/unit/test_config.py、tests/integration/test_cli.py 当前不存在，先向 A 申请明确纳入本 Issue 范围，再在本 Issue 内新建，并与 C5 对齐。
本地验收命令：本手册 §0 的平台 Black/Pylint/pytest；实现后验证 python -m g15 validate --config configs/default.yaml 的实际退出码 0，以及非法配置实际退出码 2；在实现前不可把此命令标为成功。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：现状、文件清单、§4/§10 与 A/C4 待审批差异、测试授权，等我确认。最后五项：Issue/完成、变更文件、实跑命令/退出码或未运行、风险/依赖/待审批、下一步与 PR 标题。
```

## 7. C4：同一角色、两条独立提示词（#3 与 #11）

**适用工具**：GLM 5.3。C4 要做两个 Issue，但**不能一次提示词同时实施**：第 1 波先 #3，待 #3 与 #5 的契约可用后第 3 波做 #11。传入相应 Issue 和 §5/§6 内容，不用一个大上下文混改两个模块。

### 7.1 C4 的 Issue #3：Schema、NPZ/Manifest

```text
你是 G15 的 C4，本次只做 Issue #3「NPZ 分片与 Manifest 序列化契约」；先回报不要修改。读 AGENTS.md、docs/development.md、members/C4/AGENT.md、Issue #3、docs/interface.md §4–§6、§11、当前 schema.py/serialization.py/测试；看不到就请求粘贴。
本次只允许修改：src/g15/pipeline/schema.py、src/g15/pipeline/serialization.py、tests/unit/test_serialization.py（当前不存在；本 Issue 内新建）。
你的角色长期白名单见 members/C4/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§5.1 四个等长数组的 dtype/seq/单位、§5.2 Manifest 字段/status/parts/SHA；§6 write_chunk_atomic/read_chunk 拟定接口；§11 run_dir。公共类型归属/签名须 A 评审，不得擅改 docs/interface.md、CLI 或配置键。
必须实现：同目录临时文件与原子替换、SHA-256、np.load(..., allow_pickle=False)、坏哈希/缺字段/dtype 不对/seq 不连续/路径逃逸拒收、失败不发布 complete；np.savez 用已打开的二进制文件句柄，防临时文件名被自动加 .npz。
必须补测试：在本 Issue 内新建 tests/unit/test_serialization.py；覆盖原子写、坏 SHA、字段和 dtype 错、路径越界与 pickle 禁止；不能把测试数写成已经通过。
本地验收命令：本手册 §0 的所属平台 Black/Pylint/pytest；可以在实际测试文件创建后运行针对该文件的 pytest，并记录退出码。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：分支/现状、Issue 范围、待 A 审的公共类型与失败处理、测试设计，等我确认。最后五项：Issue/完成、变更文件、真实测试命令/退出码或未运行、风险/依赖/待审批、下一步和 PR 标题。
```

### 7.2 C4 的 Issue #11：producer 生产入口

```text
你是 G15 的 C4，本次只做 Issue #11「仿真生产入口 producer」；不要把 #3 的文件带进本次提交，先回报不要修改。读 AGENTS.md、docs/development.md、members/C4/AGENT.md、Issue #11、docs/interface.md §4–§7、§11，以及已合并的 #3/#5 实际代码；看不到就请求粘贴。
本次只允许修改：src/g15/pipeline/producer.py。
你的角色长期白名单见 members/C4/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§6 produce_run(config, output_root) -> run_dir 拟定接口；§4 生产者配置/seed；§5 NPZ、完整 Manifest 与仅供验证器的 private_truth；§7 调用 B1 仿真及 C1 故障，§11 run_dir。依赖 #3、#5；#6 对突发场景也要联调，不在 producer 重写别人的算法。
边界：全分片生成后才原子发布完整 Manifest；parts 行数合计与 total_points 一致；真值去重并单独存放；写入失败不得留下被误认为 complete 的状态；不能改 schema.py/serialization.py、CLI 或任何他人模块。
必须补测试：成功 run_dir、末尾不满分片、失败不发布、真值不进公开数据；本 Issue 的文件范围没有列测试路径，先向 A 请求明确授权后才在本 Issue 内新建必要生产入口测试，当前不存在，不偷改 #3 的测试。
本地验收命令：本手册 §0 的所属平台 Black/Pylint/pytest；依赖模块未合并则写“未运行/阻塞”，不可声称链路已通。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：分支/代码现实、#3/#5（及 #6）依赖、只改 producer 的方案、需批的测试文件，等我确认。最后五项：Issue/完成、变更文件、实跑证据或未运行、风险/依赖/待审批、下一步和 PR 标题。
```

## 8. C5：独立验证与报告（Issue #9）

**适用工具**：GLM 5.3；把报告 §11、检出规则 §8.5 和实际测试结果分批输入，不给模型捏造截图或统计值的空间。

```text
你是 G15 的 C5。只处理 Issue #9「独立验证与报告」；先回报不要修改。读 AGENTS.md、docs/development.md、members/C5/AGENT.md、Issue #9、docs/interface.md §5.3–§5.4、§8.5、§9、§11、§14 和现有 reporting/tests/docs；看不到请我粘贴。
本次只允许修改：src/g15/reporting/*、tests/integration/*、tests/fault_injection/*、docs/test_plan.md、docs/test_report.md。
你的角色长期白名单见 members/C5/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：仅独立验证器在计算预设故障事件检出率时读 §5.3 真值；§5.4/§11 要输出 reports/summary.json 和 summary.md；§8.5 按事先确定的事件匹配窗口统计检出率、漏报/误报；§9 的消费数据由 C2 提供。未知值写 null，不写假 0/100%。
边界：消费者与分析层绝不能看到真值；健康样本须计误报；未经实测不填耗时与系统证明；不能删失败测试、私调阈值或改别人模块。≥15 单元测试由全组实现者共同提供，C5 汇总而非代写全部。
必须补测试：正常/退化/突发的独立生产消费报告、业务故障匹配、坏 SHA 拒收、Linux kill -9 恢复；tests/integration/* 和 tests/fault_injection/* 当前仅 .gitkeep，具体用例在本 Issue 内新建。≥20 用例、≥3 独立故障注入均需真实结果、不可用测试列表冒充通过。
本地验收命令：本手册 §0 的所属平台 Black/Pylint/pytest；全链未通时报告相关测试未运行。性能与双 Linux 结果需 B1/C6 原始证据，不能由 AI 推算。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：分支/现状、依赖 #6/#8 与 #11 状态、报告字段与验证口径、拟建用例清单，等我确认。最后五项：Issue/完成、变更文件、真实测试证据与退出码或未运行、风险/依赖/待审批、下一步与 PR 标题。
```

## 9. C6：部署脚本与双 Linux（Issue #10）

**适用工具**：GLM 5.3；提供 §10/§12、脚本现有内容和两台机器的真实环境记录，AI 不可假称已登录机器。脚本没有可执行位，**只建议 `bash scripts/xxx.sh`**。

```text
你是 G15 的 C6。只做 Issue #10「部署脚本与双 Linux 准备」；先回报不要修改。读 AGENTS.md、docs/development.md、members/C6/AGENT.md、Issue #10、docs/interface.md §10–§12、当前 scripts/* 与部署/运维文档；看不到请我粘贴。
本次只允许修改：scripts/*、docs/deployment.md、docs/operations.md。
你的角色长期白名单见 members/C6/AGENT.md；本次不要碰白名单里与本次任务无关的文件。
输入/输出契约：§10 将来 CLI 的命令和退出码，§11 的 run_dir，§12 Ubuntu 24.04 LTS 与 RHEL 9 全新克隆部署和故障恢复。当前脚本全是退出 1 的“未实现”占位、Git 索引为 100644；不能直接 ./scripts/setup.sh，不得把设计文档的未来命令写成已通过。
边界：第 1 天先起两台 VM 并确认 Python 3.11/3.12；setup.sh 建 .venv，demo/verify/benchmark 以后在各系统实跑并留安装/命令/退出码证据；强杀恢复按真实平台测试；clean.sh 不默认删用户数据；不改业务代码、CI、README 或依赖列表（需 A 单独批准）。依赖未锁定时明确不能称可复现交付。
必须补测试：setup 重复运行、失败退出非零、空 runs 下的演示、Ubuntu 与 RHEL 各自日志；若拟新增 tests/integration/test_install_smoke.py（当前不存在）等文件，先申请本 Issue 授权，本 Issue 内新建或协调 C5，不能自行越界。
本地验收命令：先按 §0 运行所属平台 Black/Pylint/pytest；脚本实现后分别在 Linux 新克隆里用 bash scripts/setup.sh、bash scripts/demo.sh、bash scripts/verify.sh、bash scripts/benchmark.sh 实测并记录退出码；当前占位运行失败只可标“未实现”，不可报通过。可执行位修复已由 A 单独开 Issue 交 C6 处理，不在本任务私做 git update-index --chmod=+x。
终端质量命令（先按 §0 安装虚拟环境；在仓库根目录仅复制本机对应的三行，Windows Git Bash/PowerShell 通用；如本 Issue 不涉及源码也需如实说明是否运行）：
Windows Git Bash/PowerShell（仅复制以下三行）：
.venv/Scripts/python.exe -m black --check src tests
.venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
.venv/Scripts/python.exe -m pytest -q
Linux（仅复制以下三行）：
.venv/bin/python -m black --check src tests
.venv/bin/python -m pylint src/g15 --fail-under=8.5
.venv/bin/python -m pytest -q
✅ 成功标志：每条实跑退出码 0；这只说明相应检查通过，不代表业务/性能/双 Linux 验收；未运行写未运行。
先回报再动手：脚本/分支现状、两台 VM 实际版本、依赖 CLI 的阶段性方案、需审批的文件与风险，等我确认。最后五项：Issue/完成、变更文件、每台机器的真实命令/退出码或未运行、风险/依赖/待审批、下一步与 PR 标题。
```

## 10. 发给 AI 后如何验收其回答

1. 确认 AI **先列事实、修改范围、待审批项，再等待你的许可**；若模型直接改了非本 Issue 文件，让其停手并核查差异，不要盲目提交：

   ```sh
   git status --short --untracked-files=all
   git diff --check
   ```

   ✅ 成功标志：真实变更只在本次获准路径内且 diff 检查为 `0`；包括新建测试也须经 Issue 授权。

2. 自己核对模型声称的测试记录；未在终端看到实际退出码的项目一律写“未运行”，先在本机按 §0 的平台命令运行。当前脚本还未实现、CLI 还未实现，不可用它们输出“已验收”。核对分支：

   ```sh
   git branch --show-current
   ```

   ✅ 成功标志：工作在自己的 Issue 分支；最终五项报告覆盖完成内容、文件、真实证据、风险和下一步，才能请真人 Review。
