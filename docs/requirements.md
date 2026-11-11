# G15 软件需求与验收追踪

> Issue [#2](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/2)；负责人 A。
> 2026-11-10 依据教师新评分标准由
> [Issue #142](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/142) 更新；实现核对基线为
> `main@997acaebef2d4ab72639954afe8078bd9a04452b`。本次只更新追踪与安全边界，
> 不改变公共接口、算法规则或 seed，不把待执行项写成验收通过。

## 0. 当前需求状态与新增强制约束

### 0.1 R01–R11 当前判定

| 范围 | 当前判定 | 剩余完成定义 |
|---|---|---|
| R01–R06、R08 | 主链实现已进入 `main` | 在最终候选 SHA 执行配置→仿真→消费→报告并保存原始结果；不能只凭文件存在判通过 |
| R07 | 报告层真值对账与指标实现已有 | #56 先冻结未参与开发的保留集，#9 再首次独立执行；此前不得称正式检出率验收完成 |
| R09 | 幂等、事务恢复、坏数据与 Linux 强杀已有实现/历史证据 | 最终候选 SHA 现场复演，和不中断基线逐表对账 |
| R10 | setup/demo/verify/benchmark 与双 Linux 历史证据已有 | 对最终候选 SHA 做 Ubuntu/RHEL clean clone 与禁网运行复验 |
| R11 | 查询治理、ADR 与候选契约已入库 | #135 服务/认证授权/稳定分页已实现（#149）；#136 Gate 0 之后才可逐档讨论容量，100k 当前未授权、未运行 |

下方 §§2–4 保留 2026-10-30 的详细追踪与历史评审上下文；其中“仍为空”“候选未合入”或旧 SHA
不再代表当前实现状态。当前发布判定以本节、[验收 V2](acceptance.md) 和
[合规追踪矩阵](compliance.md) 为准。

### 0.2 新增合规与安全需求

| ID | 强制需求 | 可验证完成条件 | 当前状态 / 责任 |
|---|---|---|---|
| S01 | 凭据不得明文存储或记录 | 当前无密码功能；若新增口令，使用 PBKDF2-HMAC/等效强哈希、随机盐、恒时比较；仓库/YAML/日志秘密扫描无明文 | 当前不处理密码；#135 使用 bearer/HMAC（非口令），已由 C4 复核；未来口令另审 |
| S02 | 安全审计只增不改 | 独立审计存储只能 INSERT；UPDATE/DELETE 被数据库约束拒绝；事件具备顺序/完整性与篡改负向测试 | **已实现（应用级只增，ADR-0008/#143/#147）**；`audit_events` + 三触发器，C4 非作者复核已通过。拥有文件/DDL 权限者仍可替换数据库，文件级防篡改未声称 |
| S03 | 工业数据不出域，外发须脱敏和授权 | 数据分级、允许字段、脱敏结果、授权人、用途和期限可追踪；真实数据不进公开仓库/第三方 AI | 仿真主链本地运行；制度/模板与自动扫描待补，A/C6 负责 |
| S04 | 真实设备接入须变更单、旁路与双人复核 | 当前仓库无设备适配器/执行器；若未来新增，必须先完成变更单、只读旁路、回退与两位非作者批准 | 当前架构不适用且禁止接入；A 门禁 |
| S05 | 停机/维护执行须人工确认 | 产品只告警、不控制设备；任何未来执行命令需独立授权、明确确认、审计和回退；自动测试证明告警不会产生控制动作 | 当前无执行器；`reset-alarms --yes` 仅重置告警状态，不等于停机功能 |
| S06 | 教师现场故障可恢复且结果可对账 | 断网、坏数据、`kill -9` 均有注入步骤、失败/降级、恢复命令、实际结果和不变式对账 | 坏数据/强杀有历史证据；断网最终 SHA 实测待 C6/C5 完成 |
| S07 | 贡献和评审真实可核 | 每人有子系统、有效 commit/PR/Review 与讲解；`git shortlog` 和周频率矩阵可复查，不刷数 | 最终冻结时由 A 汇总，个人确认；自动代理不能代签 |

## 1. 来源、范围与判定方法

需求来自课程要求摘录、A 于 2026-10-16 确认的小组默认方案及 [验收规范](acceptance.md)。原始教师材料不随公开仓库提交；NPZ、SQLite、Ubuntu 24.04 LTS/RHEL 9 是小组工程选择，不能写成教师逐项指定。答辩日期未定，D1–D14 保留为相对排期模板。

正式环境为 Conda `pp03` / Python 3.12，安装步骤见 [environment](environment.md)；Python 3.11 为额外兼容 CI。公开 GitHub 仓库、现场新克隆运行、双 Linux 分别留证等约束保持不变。

本页区分三类证据：**主线实现**说明已进入上述提交；**候选交付**说明仍在 PR 中；**正式验收**须按同一集成版本及规定环境实际执行。测试文件存在、单次 Windows 结果或 CI 绿均不能独自证明整项需求通过。

## 2. 用户故事、实现证据与剩余缺口（2026-10-30 历史追踪）

| ID | 用户故事与可验收结果 | 主线实现与可核对入口 | 尚待完成 / 负责人 |
|---|---|---|---|
| R01 | 演示者能运行正常、缓慢退化、突发故障三类预设 | 配置 [#26](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/26)、仿真 [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15)、故障 [#27](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/27)、生产 [#30](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/30)；见 `test_config.py`、`test_faults.py`、`test_producer.py` | C3/B1（承接原 C5）完成 CLI 与三场景完整链报告；已有 Python 模块不等于演示链验收完成 |
| R02 | 操作员只改 YAML 即可改变退化率和故障参数，无效输入被拒绝 | `config.py`、`configs/` 与配置/仿真/故障单测；采样率上界修复 [#47](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/47)、[#55](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/55) | C3/B1/C1/C5 在最终集成版本留下配置 A/B、源码未变、输出变化及非法配置命令记录 |
| R03 | 测试者能按配置、seed、源码和依赖版本复现测量 | 仿真确定性及噪声黄金值 [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15)、[#33](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/33)；共享类型/RNG [#31](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/31) | A/C6 的依赖锁定 [#28](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/28) 与跨环境复现；run_id/报告时间不作为测量数组相等条件 |
| R04 | 评审者可核对完整 1,000,000 点生产严格小于 60 秒 | 真实生产入口及 Windows 三次原始证据 [#53](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/53)、[ADR-0001](adr/0001-simulation-model-and-determinism.md) | C6 执行双 Linux 各三次并补进程外内存与计时范围证据，B1（承接原 C5）汇总；不重列已交 Windows 证据；[#51](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/51) 的口径补充仍 open |
| R05 | 下游在独立进程读公开文件，不依赖仿真内存状态 | 序列化 [#29](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/29)、生产 [#30](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/30)、消费 [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64)；`test_serialization.py`/`test_consumer.py` | C3 的消费 CLI [#71](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/71) 仍为候选；simulate 与 consume 分进程命令须在集成后留证 |
| R06 | 维护人员看到特征、0–100 健康分与可解释告警 | 分析 [#54](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/54)、消费/SQLite [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64)；`test_features.py`、`test_health.py`、`test_alarms.py` | B2 的数值边界修复 [#73](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73) 已提交整改候选 `e4814b65769fa6da45ca070daac89af02d03d0e9`，A已在该 head 复审APPROVED，B1 已于[5700184005](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5700184005) 复审 APPROVED，C4已于[5701409871](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5701409871)复审APPROVED，仍待C2相关审查与集成；契约 [#57](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57) 与报告展示待闭环 |
| R07 | 评审者能独立核对受控事件检出、漏报、误报和延迟 | 验收规范与 [ADR-0006](adr/0006-independent-validation-protocol.md) 已有审阅方案；主线 reporting 仍为空模块 | 验证/报告候选 [#70](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70) 未合入；[#56](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56) 的独立数据方案/规则冻结与正式执行未完成 |
| R08 | 项目负责人获取可追溯 JSON/Markdown 闭环报告 | 主线已有 Manifest 和 SQLite 上游；`docs/test_report.md` 仍占位 | 当前 #9 报告实现由 B1 接手，候选为 [#70](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70)；集成后由相关实现/验证角色留版本、点数、耗时及错误证据 |
| R09 | 运维人员重复消费或中断重启后不重不漏 | [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64) 提供消费、存储、状态恢复与幂等单测；[ADR-0005](adr/0005-analysis-persistence-contract.md) 记录契约提案 | C2/C6/C5 补真实 Linux `kill -9` 与不中断对照；不能把单元回滚当进程强杀或断电测试 |
| R10 | 首次接手者在两种 Linux 同一版本新克隆安装运行 | [environment](environment.md) 有环境约定；[#59](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/59) 仅修复 shell 文件可执行位 | C6/A 完成 [#10](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/10)、#28；当前 setup/demo/verify/benchmark 仍为失败退出的占位脚本 |

表内测试文件均位于 `tests/unit/`，只标识可检查入口，不声称本次重新运行。

## 3. 非功能需求与统计口径（保留口径，状态见 §0）

| ID | 保持的要求 | 当前证据边界与补齐方式 |
|---|---|---|
| N01 | 1,000,000 个采样时刻；生成、序列化、哈希和 complete Manifest 发布合计严格 <60 秒；双信号共 2,000,000 标量另报 | 沿用 acceptance §2，双 Linux 各三次；#53 的 Windows 结果不替代 Linux，不把内核计时或事后校验混进生产窗口 |
| N02 | 预设受控集事件召回 100%，同时报告误报和延迟；零事件召回为 null | 保持 acceptance §3；开发已查看数据与独立保留验证分开，#56 未冻结前不选/跑新保留 seed |
| N03 | 配置/seed/版本可追溯；UTC、绝对序号和单位明确 | 保留仿真确定性；最终记录源码、配置/规则摘要与依赖，不以当前机器时钟生成测量 |
| N04 | 参数/配置 2，I/O/Schema 3，实际验收失败 4，未知错误 5，成功 0 | 沿用 interface §10。`cli.py` 已定义 `EXIT_OK=0`、`EXIT_INVALID=2`、`EXIT_IO=3`、`EXIT_ACCEPTANCE=4`、`EXIT_UNEXPECTED=5`；`validate`、`simulate`、`consume`、`report`、`benchmark` 已接线，`verify`、`doctor` 尚未接线并返回 5。I/O/Schema 失败使用 3；4 只用于实际运行且未通过的验收门禁（当前 `benchmark`，未来 `verify`），不得扩到普通 `report`；报告产物成功生成即返回 0，即使报告字段为 `passed=false` 或 `passed=null` |
| N05 | 安装可联网，业务运行不依赖网络或在线 AI | C6 实测现场网络、运行离线与备用依赖；尚未完成的离线安装准备保持待办 |
| N06 | 数据、告警状态、分片游标同事务；重复/恢复可诊断 | #64 已有实现与回归；真实强杀、日志和重启后内容一致性仍需实测，不宣称断电零损失 |
| N07 | 至少 15 单测、20 个可执行用例、3 类故障注入证据 | 测试存在及数量不替代执行结果；三类为业务故障、文件损坏、Linux 强杀恢复，不能用三种波形替代三类可靠性证据 |
| N08 | Ubuntu 24.04 LTS 与 RHEL 9 同一集成提交分别安装运行留证 | CI 的 Python 3.11/3.12 矩阵不等于两种 Linux；当前不声明此项已通过 |

旧文中 main `2c14759` 的“收集 88 个用例”只适用于早期快照，不作为当前统计。本次不引入新的通过总数；每次结果须绑定自己的源码 SHA、环境、命令、退出码与跳过原因。A 的历史实测归档见 [#75](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75)、[#78](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78)，两者已分别经B1/C4人工审阅合入（#75：`5e98a68aa857241d36211a288d6fc804b8331ffb`；#78：`93fcf287c90750dc8331bee55245ab63bfc1d156`），是历史归档而非正式验收报告。

## 4. 2026-10-30 候选与变更控制历史

| 候选 | 已交付的对象 | 保留的条件 |
|---|---|---|
| [#67](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/67) | 持久化、校验分层与 CLI 接线的接口正文 | 成员实际审阅与 §13 兼容证据；本页不提前冻结候选 |
| [#70](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70) | 独立验证器与闭环报告 | B2提出场景类型依赖名称的风险，A独立复证并请求修改；A已裁定采用受审清单精确id/未知不可验证方案C，等B1按裁定提交新head与回归，其他集成与独立验收条件保留 |
| [#71](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/71) | consume CLI 接线 | C2 对公开签名/异常集合的本人确认等门禁 |
| [#72](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72) | 原始分片独立校验 | R5 已获 A 与 B2 本人复审 APPROVED（B2：5684239091）；C4已COMMENTED三块通过但正文范围/历史数字待作者补，C3与相关接口条件仍保留；七项检查不核绝对时间公式 |
| [#73](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73) | 特征数值稳定性修复 | #73 已合入 `0a637fc2`，A/B1/C4/C2 均已在 `e4814b65` 批准，FEATURES_VERSION 1.1 成为主线事实；#82 已由 #91 合入 `51cd99ad`，1.0/1.1 时期部分掩码与全掩码 float 被静默使用、全掩码整数在 1.1 泄漏 MaskError 的历史行为，已在 1.2 改为掩码非空即 `ValueError` 拒收，空掩码继续接受。性能、双 Linux 与检出率验收边界不因该合入完成 |

#76 多片末片上界预检已获A/B1/C4固定head审阅并合入主线`b3daf15ee8d53d7515f32b13b57de149f6767561`；超界在业务写入前拒收、等长允许，单片不反推。#67接口现状同步仍须审查，不把代码集成当作接口冻结。

公共接口按 [interface §13](interface.md#13-接口变更流程) 处理；最终成绩按 [acceptance](acceptance.md) 执行。A 汇总实际意见和缺口，模块负责人补实物，不能互相代签。#17 的共享类型迁移已经由 #31 合入，不能继续列为未实施；#2/#57 仍保留当前持久化等接口收口任务。

## 5. 增量工作假设：十万用户查询（提案，未冻结）

> 关联 [Issue #133](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/133) 与 [ADR-0007](adr/0007-user-query-capacity-and-validation.md)；提案基线为 main `23d8592839cd4ad8f0e6006464e10efe3afa6234`。本节来自组长对课堂“用户侧、高并发、可溯源日志、全流程交付”侧重点的工作判断，不是教师已确认的评分细则或权重。
>
> 主目标已经确定为：100,000 个不同且已认证、获授权的用户，在同一受控波次中发起真实业务查询，并以客户端与服务端实际在途状态证明“同时查询”。Review 只精确化同步窗口、统计区间、SLO、资源和阶段边界，不再选择 H 或 F。

### 5.1 新增用户故事

| ID | 用户故事与可验收结果 | 当前事实 | 待评审 / 责任 |
|---|---|---|---|
| R11 | 100,000 个不同且已认证、获授权的操作员在同一受控波次中，各按其可见的 `run_id + device_id` 发起一条真实查询；查询返回最近一页健康窗口及告警，稳定分页、可追溯且不读取 `private_truth`，越权和错误结果为零；客户端与服务端分阶段实证实际在途 | （2026-10-30 状态）当时只有 CLI、SQLite、`connect_readonly()` 及内部报告查询；#149 已合入只读授权查询、HMAC bearer、稳定分页与脱敏 trace；缓存/10 万容量仍未实现 | A/B1 共同负责新增需求、实现和验证；C4 做必要的非作者/契约 Review，A 做最终门禁。新增路径须由实现 Issue 明确白名单并同步 CODEOWNERS |

R11 的第一条真实路径必须读取已提交的 `runs / feature_windows / alarms / alarm_states`，并与独立 SQL/oracle 对账；禁止以常量响应、空 handler、`sleep`、mock 数据库或 HTTP 200 造容量成绩。认证失败、无权限、未知设备、无完整窗口和数据版本变化均须有明确行为且不得泄露资源存在性。

### 5.2 新增非功能候选

| ID | 待评审候选 | 当前证据边界 |
|---|---|---|
| N09 | 主目标固定为 100,000 个不同已授权用户在同一受控波次发起真实主请求；报告必须按用户去重并分列 `client_started/client_waiting/client_completed` 与 `server_received/server_queued/server_executing/server_completed`，分别计算客户端/服务端实际在途峰值 | 同步窗口、统计区间、时钟误差、SLO 和资源尚待 Review；连接、累计请求、重试、VU、百万点生产、三进程消费压力或 SQLite WAL 均不能替代 |
| N10 | 主场景 F 使用发送 barrier 与同一 request/trace ID 实证同步发起和实际在途；只有某阶段的不同用户峰值真实达到 100,000 才能声称该阶段达到 100k；禁止 `sleep`、人工变慢 handler、压住/延后响应或扩大队列造峰值 | 阶段判定、同步/统计窗口、候选 SLO、分布式负载端、网络/服务容量、资源和费用另审；当前不授权购买资源或执行十万级负载 |
| N11 | 补充场景 H 用于带 think time 的持续活跃用户与 soak；关闭式稳态吞吐近似 `X_closed ≈ N / (think_time + mean_response_time)`；100,000 用户、30 秒 think time 的 3,333.3 req/s 仅在响应时间趋近 0 时为近似上限；固定到达率须另用开放式模型 | H 不是主目标的替代验收，任何结果均不得降低或宣告 F 通过 |
| N12 | UTC request/trace ID、匿名化 actor/权限域、数据版本、workload/cache 状态、状态/错误码、端到端/DB 时延及服务端/压测端资源可溯源；凭据、敏感数据和真值禁止入日志 | 当前 `common/logger.py` 为空，既有运行日志不是请求级证据 |
| N13 | 单元 → 真实集成 → 1/10/50 低负载冒烟/回归 → Windows/双 Linux 兼容 → F 的 100/500/1000 性能 → 1k/5k/10k/25k/50k/100k 不同用户实际在途压力；每档满足正确性、阶段计数可信和两端资源余量才升级，H 另行报告 | 当前只批准规划和文档 Review，不执行十万级；所有失败、固定 SHA、命令、环境、原始直方图和两端资源必须保留 |
| N14 | 查询实现前冻结授权扇出、稳定分页、具名版本、提交态新鲜度、审计投影、正式只读连接及查询计划/索引 | 由 #135 固化真实查询契约、#136 建立 oracle 与分级门禁；按现行“每设备一 run、每 run 一库”和代表性 D0/D1/D2 保存 `EXPLAIN QUERY PLAN` 与冷/热态证据。不得把合成单库探针外推为生产结论，也不得在真实 SQL 与数据分布冻结前预定新增索引 |

按现行 `benchmark_1m.yaml` 的 1,000,000 点、`chunk_points=102,400` 与 `consumer.yaml` 的 `window_points=256`、`incomplete_window=drop`，每 run 为 3,906 个完整窗口并丢弃尾部 64 点。候选数据集为 D0（10 设备 × 100 窗口）、D1（1,000 设备 × 3,906 窗口，共 3,906,000 / 390.6 万行）和 D2（10,000 设备 × 3,906 窗口，共 39,060,000 / 3,906 万行）。现行 Manifest 契约是一 run 一 device，且每个 run 使用独立 `results.sqlite`：D1/D2 是分别分散在 1,000/10,000 个数据库中的 `feature_windows` 汇总规模，每库约 3,906 行；告警、状态与历史 run 另计。若改成共享读模型或单库多设备布局，须另审 Schema、迁移、目录/聚合策略与代表性查询计划。设备/用户比、历史 run 数、告警密度、响应大小、缓存冷热与保留周期仍待评审；D2 在磁盘、建库时间、路由成本和 RSS 未测前不得称为可行。

### 5.3 查询实现前冻结项（#135 / #136，摘要；完整 13 条见 ADR-0007 §5）

1. **授权扇出**：每用户/角色/租户可见的 device/run 数量与上限、总映射规模、热点分布、历史 run 和跨 run 路由/分页上限。
2. **稳定分页**：冻结查询范围、确定性全序、游标字段、页大小、方向和提交态可见性；窗口可评审 `start_seq`，告警须用 `start_seq + rule_id` 或等价唯一键打破并列，不以仅 OFFSET 冒充稳定游标。
3. **具名版本**：逐项区分 Manifest `schema_version=2.0`、配置契约 `SCHEMA_VERSION=1.0`、特征信封 `features_version`、状态信封 `state_schema_version`；`scoring_version` 与 `rule_id` 保持独立含义。只有存在可追溯持久化来源时才返回配置版本，不得从 Manifest 推断或用笼统 `data_version` 混写。
4. **审计投影**：普通健康查询只按允许列表返回当前业务 `state` 与获准版本字段；不得返回原始 `state_json`、`reset_event`、`reset_history`、`previous_state_json` 或自由文本 `reason`。审计查询若有需求另设授权入口。
5. **提交态新鲜度**：只依据同一只读事务可见的已提交行、序号、状态或持久化时间；不得读取 `private_truth` 或用真值注入时间推导。跨库查询须分别记录每库 `as_of`，未实现共享快照前不得宣称全局原子新鲜度。
6. **正式只读路径**：请求层使用现有 `connect_readonly()` 打开已存在数据库，不得调用可写连接、初始化 Schema、执行 DDL/DML，或在请求路径运行会写统计信息的 `ANALYZE`。
7. **查询计划与索引**：针对冻结后的真实 SQL、当前一 run 一库拓扑和代表性数据分别保存 `EXPLAIN QUERY PLAN`、冷/热态与统计信息证据，再决定沿用现有索引、在维护阶段生成统计、另立索引变更或引入聚合读模型。

C4 在 [#133 合入后复核](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/133#issuecomment-6762781638) 中使用单 SQLite、单 run、多 device、40 万条 `alarms` 的合成探针；未 `ANALYZE`、已 `ANALYZE`、再加复合索引三种状态同时改变了统计与索引条件。该结果只提示查询计划可能敏感，不代表现行一 run 一 device / 每 run 一库的生产性能，更不预定新增复合索引。

任一错误/越权/跨权限缓存泄漏、client/server/request-log 计数不守恒、服务崩溃/OOM/数据损坏、SQLite 锁错误持续增长、压测端饱和或统计/缓存状态不可证明，均停止升档并留存失败。

### 5.4 变更与保密边界

- 本节不改变 R04/N01 百万点生产、R07/N02 与 #56/#9 独立效果验收、Schema/真值隔离、事务或双 Linux既有口径。
- 现有成员模块属地不因本节改变；新增 query/service/auth 路径由 A/B1 承接，若需修改现有 storage、analysis、CLI 或 scripts，另开实现 Issue 并取得对应 Review。
- `syc` 为 `src` 的笔误；`src` 与 `test` 的对应关系按 B1 复核记录，不再作为待定术语。
- 外部课堂参考项目及第三方相关信息不进入公开仓库、fixture 或日志；概要/详细设计、TOGAF 映射和 Windows/Linux 部署材料只从 G15 自有已审事实派生。
- 关闭 #133 或合入 ADR/需求文档只表示方案可继续评审，不表示服务已实现、阈值已冻结或十万级已通过。
