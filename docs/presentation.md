# G15 期末答辩与现场演示手册

> 适用基线：`main@997acaebef2d4ab72639954afe8078bd9a04452b`，2026-11-10。
> 本手册是 Issue #142 的答辩组织稿，不是通过证明。现场成绩、独立保留集、
> 查询容量与尚未运行的命令一律以当场输出和固定 SHA 证据为准。

## 1. 答辩前的事实边界

答辩开场先说明三件事，防止把课程沙盘说成真实工业产品：

1. 本项目模拟轴承振动与温度，验证的是可复现的教学闭环，不代表真实设备故障诊断精度。
2. 当前主链是 YAML → 仿真/故障 → NPZ/Manifest → 独立消费 → 特征/健康分/告警 → SQLite → JSON/Markdown 报告；消费者和分析模块禁止读取真值。
3. 当前系统只产生告警，不连接执行器、不自动停机。任何未来真实设备接入都必须先有变更单、旁路方案和两名非作者复核；任何停机动作必须由人确认。

答辩候选 SHA 必须在演示前填写，且与测试、双 Linux、性能、贡献快照一致：

| 项 | 现场填写 |
|---|---|
| Release / commit SHA | `待冻结` |
| Ubuntu 证据 | `待在候选 SHA 复证` |
| RHEL 证据 | `待在候选 SHA 复证` |
| 独立保留集结论 | `#56 → #9，未完成前不得填“通过”` |
| 查询 Gate 0 / p95 / EXPLAIN | `#135 已实现（#149）；#136 Gate 0 未执行，未完成前不得外推 100k` |
| append-only 审计 | `独立存储 Issue 待实施；现有 reset_history 不等价` |

## 2. 四段论主线（建议 12–15 分钟）

### 2.1 业务问题（约 2 分钟）

- 工业设备等到彻底损坏再维修，代价是非计划停机、备件和安全风险。
- 本项目用可控仿真代替不可获得的真实设备试验台，生成趋势、噪声和 spike/step/burst 事件。
- 业务输出不是“自动停机”，而是可追溯的健康分、告警和报告，供人决策。
- 成功标准包括：百万点完整生产 <60 秒、受控事件逐一对账、正常场景误报披露、坏数据拒收、进程强杀后幂等恢复、换机可部署。

### 2.2 架构与分工（约 3 分钟）

讲解时沿数据流走，不按文件名朗读：

```text
configs/*.yaml
  → config / CLI
  → simulation (degradation + noise + controlled faults)
  → pipeline producer (NPZ shards + complete Manifest)
  → [process/file boundary]
  → consumer + analysis
  → SQLite transaction (windows + alarms + state + processed chunk)
  → reporting (JSON + Markdown; truth reconciliation only here)
```

四个必须说清的设计理由：

1. **文件解耦**：生产和消费是独立进程；Manifest 最后发布，未完成运行不可消费。
2. **真值隔离**：`private_truth` 只允许报告层对账；完整性校验器、消费者和 analysis 不读答案。
3. **事务恢复**：窗口、告警、状态和分片完成标记同事务；重启从已提交分片继续，同片异 SHA 拒收。
4. **证据分层**：CI 绿、Windows 预检、双 Linux、独立验证和教师现场注入是不同证据，不能互相冒充。

### 2.3 现场演示（约 5–7 分钟）

演示前打印身份，不从旧 `runs/` 复用结果：

```bash
git rev-parse HEAD
git status --short
python --version
python -m pip check
```

#### A. 最短闭环

在已按 `docs/environment.md` 准备好的 `pp03` 环境中执行：

```bash
bash scripts/demo.sh
bash scripts/verify.sh
```

现场指出：脚本任一步失败都必须非零退出；报告中的 `null` 表示不可判定，不得改写成 0 或 100%。

#### B. 配置外置

展示两个已入库 YAML 的退化率、seed 或故障设置不同，而 Python 源码 SHA 不变。先运行 `validate`，再分别运行；不在现场临时改阈值追求通过。

#### C. 教师故障注入

按教师选择执行一项或多项；每项都先说预期，再执行，再展示恢复后的恒等关系：

| 注入 | 现场命令/入口 | 预期与恢复判据 |
|---|---|---|
| 断网 | 依赖预装后关闭网络，再运行 `demo.sh` / `verify.sh` | 运行期不访问网络；主链继续。该项须在候选 SHA 另留真实禁网记录 |
| 坏数据 | `python -m pytest -q tests/fault_injection/test_pipeline_faults.py -k corrupted` | SHA/Schema 明确拒收，不把部分结果写成可信完成 |
| kill 进程 | Linux 上运行同文件的 `kill_dash_nine` 用例 | 子进程收到真实 SIGKILL；重放后五表逐行等于未中断对照 |
| 重复消费 | 相关 integration/stress 用例 | 第二次不增加窗口或告警；同片异 SHA 必须失败 |

Windows 没有 POSIX `kill -9` 语义；若现场不是 Linux，必须明确该项只能展示既有 Linux 证据或切换 Linux 环境，不能用 `process.kill()` 的 Windows 行为冒充。

#### D. 性能与技术深度

```bash
python -m g15 benchmark --config configs/benchmark_1m.yaml
```

只按 `docs/acceptance.md` 的完整生产窗口判定；展示 10 个分片、1,000,000 行、2,000,000 个信号标量、Manifest 与校验结果。查询 p50/p95/p99 和 `EXPLAIN QUERY PLAN` 属于 #136；#135 已实现，但没有 D0 数据时不得展示历史探针并外推 100k。

### 2.4 复盘与贡献（约 2–3 分钟）

- 先讲一项失败或误判，再讲如何用测试/ADR/Review 修正；不要只念“全部成功”。
- 贡献以 Issue、PR、Review、文件和可复现实测为证据；commit 数是核验维度，不是价值排名。
- 每人用第 4 节的讲解卡说明自己的输入、输出、关键边界、测试和一次故障处理。
- 最后列未完成项：#56/#9 独立验收、#136 查询容量 Gate、候选 SHA 双 Linux/禁网复证。诚实边界本身是工程能力。

## 3. 必答题与抽答题库

回答至少覆盖每题列出的要点；只给结论、不讲边界和证据，视为覆盖不足。

### 必答 1：项目解决什么问题，怎样证明不是“看答案报警”？

要点：

- 目标是教学型预测性维护闭环：从振动/温度得到健康分、告警和报告。
- 故障位置写在 `private_truth/faults.json`，不进入公开分片。
- consumer、analysis 与 `reporting.verifier` 禁止读真值；只有 `reporting.builder/metrics` 在检测结果形成后做一对一对账。
- 报告同时给 TP/FN/FP、重复/未匹配告警和检测延迟；零事件返回 `null`，不填 100%。
- 100% 只指预先冻结的受控事件集，不代表真实工业准确率。

### 必答 2：进程被 kill 或数据损坏时，系统为什么能恢复？

要点：

- producer 先写临时分片、计算 SHA，最后原子发布 complete Manifest。
- consumer 只读 complete Manifest，逐片校验字段、行数、连续序号和 SHA。
- 特征、健康分、告警、状态与 `processed_chunks` 同一 SQLite 事务提交，WAL 负责未提交事务回滚。
- 重启跳过同 ID/同 SHA 的已提交片；同 ID/异 SHA 作为契约冲突拒绝。
- Linux 真实 SIGKILL 用例要求最终五表逐行等于未中断对照；Windows skip 不算通过。

### 必答 3：100 万点 <60 秒的口径是什么，证据如何防止“偷换计时”？

要点：

- 一个点是一个采样时刻；两路信号共 2,000,000 个标量。
- 从第一次生成前到全部 NPZ、SHA 与 complete Manifest 发布并关闭文件，使用 `perf_counter`。
- 不把仅算法内核、缓存旧产物或少写字段当完整生产。
- Ubuntu/RHEL 各自空目录连续三次，记录 SHA、硬件、环境、命令、退出码、中位数、最大值和峰值内存。
- 独立校验耗时与生产耗时分列；答辩只引用同一候选 SHA 的证据。

### 抽答 1：为什么用 NPZ + Manifest，而不是直接在进程间传对象？

- 满足生产/消费解耦；消费者可独立启动、重放和审计。
- NPZ 保留数值 dtype/shape，`allow_pickle=False` 降低不可信对象风险。
- Manifest 固定版本、单位、分片摘要和完成状态；代价是磁盘 I/O 与版本治理。

### 抽答 2：健康分是不是机器学习模型？如何解释？

- 当前是配置化、可解释的启发式评分，不是 RUL 或真实工业诊断模型。
- 特征与基线/范围/权重形成风险贡献，结果裁剪到 [0,100]。
- 规则版本与特征版本要可追溯；更换规则不能覆盖旧库宣称同一结果。

### 抽答 3：断网时为什么还能运行？

- 安装阶段可能需要依赖源；依赖安装完成后，运行期只使用本地配置、NPZ、SQLite 与报告，不调用云端 AI/API。
- “设计上离线”不等于“已实测禁网”；候选 SHA 必须关闭网络执行闭环并保留命令、退出码和连接检查。
- 已实现的 HTTP 查询服务（#149）的断连、超时、重试是 #136 的另一套验收，不能用离线主链替代。

### 抽答 4：密码、Token 与日志怎样满足安全红线？

- 当前主链没有账号/密码存储，因此 PBKDF2 条款按“不处理密码”说明，绝不为展示凭空增加明文密码。
- #135 已采用运行时 bearer/HMAC；凭据不得入 YAML、仓库或日志，比较必须恒时。
- 如果未来增加口令，必须使用 PBKDF2-HMAC/scrypt/bcrypt/Argon2、随机盐和受审迁移；禁止明文或可逆存储。
- trace/actor 使用脱敏标识，日志扫描应证明无 token、PII 和真值。

### 抽答 5：现有 reset 历史为什么还不能叫 append-only 审计？

- 它保存在可更新的 `alarm_states.state_json` 中，虽然保留历史，但物理记录仍可被 UPDATE 覆盖。
- 红线需要独立 INSERT-only 事件、禁止 UPDATE/DELETE，并有篡改失败测试；该变更必须独立 Issue、ADR 和 C2 Review。
- `reset-alarms --yes --reason` 已满足显式人工确认，但不等于不可改审计已经完成。

### 抽答 6：为什么选择 SQLite，怎样看并发能力？

- 单机教学闭环需要事务、外键、可移植和低运维成本，SQLite/WAL 合适。
- 当前承诺单写者和只读查询；压力测试中的冲突拒绝不等于支持多写者。
- 分布式扩展应保持稳定查询契约，将共享读模型/服务层与现有运行库隔离；通过 ADR、p95/p99、资源与 `EXPLAIN` 决策，不凭口号改成微服务。

### 抽答 7：项目怎样对照软件工程标准？

- 需求、架构/WBS、编码、测试、部署、运维、复盘形成生命周期证据链。
- GB/T 标准是“参照与映射”，不是认证声明；具体映射见 `docs/compliance.md`。
- 每条需求连接到测试命令、阈值、SHA 和证据；未知/未执行保持待验证。

### 抽答 8：如何证明贡献真实，而不是刷 commit？

- `git shortlog` 核 commit 身份和数量，Issue/PR/Review/文件/测试说明贡献内容。
- squash、co-author、别名和代改必须结合 GitHub 记录，不能只按显示名归属。
- 每周三次和人人有 commit 是硬核查项；缺口只能由成员后续真实有意义交付补齐，不能改写历史或拆空提交。

## 4. 九角色子系统讲解卡

每人答辩前把“我的实际证据”列换成自己的 Issue/PR/SHA；没有本人证据时不得认领他人交付。

| 角色 | 输入 → 输出 | 必讲边界 | 现场可指证据 |
|---|---|---|---|
| A | 教师标准/Issue → 契约、ADR、集成门禁 | 公共接口变更须 Review；不把 AI 自审算人工批准 | requirements/architecture/ADR、CI、Review |
| B1 | 场景配置 → 确定性退化与噪声块 | 同 seed/版本可复现；性能含生产落盘 | simulation、百万点 benchmark |
| C1 | 基础信号 + 事件配置 → 注入波形 + 真值 | spike/step/burst 定义；真值不进检测路径 | faults 实现和波形测试 |
| C3 | YAML/参数 → 不可变配置、CLI 分派与退出码 | CLI 不写业务公式；未知字段拒绝 | config、validate/consume/report/reset 命令 |
| C4 | 仿真块 → NPZ 分片 + Manifest | dtype/单位/路径/SHA；Manifest 最后原子发布 | schema/serialization/producer |
| B2 | 窗口 → 特征、健康分、告警状态 | 数值有限、评分 [0,100]、去抖/恢复可解释 | features/health/alarms 与规则配置 |
| C2 | 公开分片 → SQLite 五表与恢复状态 | 同片幂等、异 SHA 拒绝、分片事务原子 | consumer/storage、kill/replay 测试 |
| C5 | 结果 + 隔离真值 → 独立指标和测试报告 | 不调参、不隐去 FP/FN；`null` 不伪成通过 | test_plan/report、故障注入与报告测试 |
| C6 | 仓库 + 锁依赖 → 可部署环境与运维证据 | Ubuntu/RHEL 分别 fresh clone；脚本失败非零 | setup/demo/verify/benchmark、部署记录 |

当前贡献台账显示 C5 本人仍没有可核验的独立 PR 条目。答辩前必须由本人补真实、有意义、经 Review 的交付，或按实际队伍与职责如实说明；不得把 B1 承接工作改署给 C5。

## 5. 演示失败时的回退预案

| 失败 | 当场动作 | 禁止做法 |
|---|---|---|
| 安装源/网络不可用 | 展示失败退出码；切换到事先验证的本地依赖缓存后重试，并注明不是 fresh online install | 临时删依赖、吞错误或声称网络正常 |
| demo 中途失败 | 保留 run/log，按退出码定位；换全新目录重试一次 | 删除失败证据、复用旧成功目录 |
| 性能超过 60 秒 | 记录全部三轮和机器负载，判该环境未通过；说明分析计划 | 只挑最好一轮、改小点数或只报内核耗时 |
| 受控故障未检出/有误报 | 展示 TP/FN/FP 和规则版本，判未通过并保留产物 | 现场调阈值或读取真值改答案 |
| kill 用例无法执行 | 确认是否 Linux/POSIX；若非 Linux，展示边界并切换正式环境 | 用 Windows 行为冒充 SIGKILL |
| GitHub 不可访问 | 展示本地 commit SHA、已签出的 clean tree 与事先固定证据；恢复后再核远端 | 把本地缓存说成现场 clone |

录像、截图和历史日志只能作为补充证据，不能替代教师要求的现场可运行仓库。任何回退都要保留原失败、执行命令、退出码和环境。

## 6. 答辩前两轮演练检查

### 第一轮：技术演练

- 每人按讲解卡在 90 秒内说清输入、输出、关键边界、测试和一次故障。
- 在无预生成 `runs/` 的环境走一次 demo/verify/benchmark。
- 随机抽两项故障注入；核对恢复后恒等式和日志，而非只看退出码。
- 逐题抽答；答案缺“证据/边界/失败处理”任一项即补讲。

### 第二轮：交付演练

- 固定候选 SHA；Ubuntu/RHEL、性能、测试报告、贡献表全部指向同一 SHA。
- 关闭网络复跑运行期闭环并留证；安装阶段与运行阶段分开陈述。
- 模拟 GitHub、投影、网络任一不可用，验证回退预案。
- 最后一页只列真实完成和真实待办，不出现“预计通过”“理论 100%”等措辞。
