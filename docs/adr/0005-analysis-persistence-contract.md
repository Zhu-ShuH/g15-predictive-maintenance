# ADR-0005：分析结果与告警恢复的候选持久化契约

- 状态：**提案，未冻结**；2026-10-23 T69更新（原提案2026-10-22）；关联 [Issue #57](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57)、#54、#8、#2。
- 准备：A/Codex，按组长“按A执行待办”更新；C2本人S1–S6答复已收到，相关角色确认与实现复核仍未完成。
- 权威基线仍为 [interface](../interface.md)、[acceptance](../acceptance.md) 与 [ADR-0004](0004-recovery-and-independent-evaluation.md)。本文供逐项评审，不覆盖基线、不授权提前按候选字段建表。

## 审阅对象与当前事实

本轮main固定为 `81c13d17faf8189a240989b4ee43b937820bb911`；#54已经squash合入，下面三算法blob与原最终head `da3a52b9105edfd0c418c7c29369fd6c68aab01d`一致。main的consumer/storage仍占位。消费候选是 [PR #64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64) 的 `e1593ff707925cf4ebee2758a332be6996783d25`，未合入，不能计为主链已交付。

2026-10-23 14:01北京时间，C2已在 [#57本人回复](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57#issuecomment-6358549345) 和 [#8输入/接线回复](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/8#issuecomment-6358549702)给出选择。回复中的“#54尚未合入”仅适用于回复时刻；A现在从真实答复推进评审，不再等C2首次回复。C2称当时没有存量库，本提案仅据main空模块与其本人说明把legacy视为防御场景，不声称完成所有环境数据审计。

候选 head 的三个模块 Git blob：

| 模块 | Git blob |
|---|---|
| features | `601d4c5aaa741682a343a98cd128077197dec4e1` |
| health | `cab284f9f2ba227fffacca75d43c3eb7d3cae982` |
| alarms | `4bfee9dcbad9e8af7fd26a4d55083daf7c0d1378` |

该 head 的分析三文件单测在2026-10-22 T65轮由 Codex 隔离环境实跑 113 passed（30/40/43），另有公开 API 连续窗口/JSON 恢复对照；这只证明本轮算法与恢复输入边界，不是 SQLite、kill -9、独立检出或双 Linux 验收。B1 已对该 head 正式批准，公共持久化契约仍待相关角色确认。

## 候选数据结构：源码观察，不是新建表决定

以下字段从固定候选源码提取；类型来自注解，是否/如何持久化仍须 C2/B2 对齐。

### FeatureRecord

| 字段 | 候选源码类型 |
|---|---|
| `mean` | `float` |
| `std` | `float` |
| `rms` | `float` |
| `ptp` | `float` |
| `kurtosis_excess` | `float` |
| `crest_factor` | `float` |
| `dominant_freq_hz` | `float` |
| `zero_crossing_rate` | `float` |
| `sample_count` | `int` |
| `sample_rate_hz` | `int` |
| `window_start_seq` | `int | None` |

### HealthResult

| 字段 | 候选源码类型 |
|---|---|
| `health_score` | `float` |
| `scoring_version` | `str` |
| `rule_id` | `str` |
| `window_start_seq` | `int | None` |
| `contributions` | `tuple[RiskContribution, ...]` |

### Alarm

| 字段 | 候选源码类型 |
|---|---|
| `rule_id` | `str` |
| `level` | `str` |
| `start_seq` | `int` |
| `health_score` | `float` |
| `scoring_version` | `str` |
| `reason` | `str` |
| `drivers` | `tuple[str, ...]` |

### AlarmState

| 字段 | 候选源码类型 |
|---|---|
| `active_level` | `str | None` |
| `consecutive_breaches` | `int` |
| `normal_windows` | `int` |
| `alarm_windows` | `int` |
| `last_window_start_seq` | `int | None` |
| `recovered_at_seq` | `int | None` |
| `scoring_version` | `str | None` |
| `rule_id` | `str | None` |

`RiskContribution` 是评分解释中的嵌套记录，不新增数据库列或表。Python tuple 如何序列化为 JSON array、恢复为原类型，须由 C2 提出明确编码/兼容规则；不能把 dataclass 的类型注解当成数据库已实现的约束。

## 已有基线与待审映射

| 对象 | 已有基线/候选行为 | 本提案的评审边界 |
|---|---|---|
| FeatureRecord | 8 个特征可由 as_mapping 得到；另有 sample_count、sample_rate_hz、可空 window_start_seq | feature_windows 现行是 features_json；8 个特征不自动变成8列。sample_count/rate、FEATURES_VERSION 的追溯存放位置由 C2/C5确认，当前 FEATURES_VERSION 仅模块元数据 |
| HealthResult | 分数、规则身份、窗口起点、贡献与 drivers | health_score 对应已有列；解释与版本追溯如何保存仍待确认，不默加数据库列 |
| Alarm | 不含 run_id/device_id/created_at | consumer 从manifest附加run/device；C2提出created_at为落库时刻UTC RFC3339 Z；唯一键保持现行run/device/rule/start，待相关角色审阅 |
| AlarmState | 六个状态量加 scoring_version/rule_id；带历史时身份和位置必需 | state_json 应能完整恢复8字段；C2提出外层state_schema_version="1"信封，详见S2；尚未冻结，实际legacy兼容与重置边界须复核 |

生产链由 consumer 从公开样本 seq 传入 window_start_seq；None 只适用于离线单窗特征/评分，不能进入告警链。保持“至少5个特征”约束提取输出的审阅解释，不新增规则引用特征数≥5的加载门槛。

## 已核实的恢复行为

候选 evaluate_alarm 以“active_level非空，或consecutive_breaches非零，或alarm_windows非零”判定有待继承历史：

| 恢复输入 | 候选实现 |
|---|---|
| 无可继承历史 | 可接受并改标当前规则身份；保留窗口单调性检查 |
| 有历史，身份齐全且相同，位置合法 | 接受并继续计数 |
| 有历史，缺 scoring_version 或 rule_id | ValueError：来源无法确定；不视作fresh、不静默改标 |
| 有历史，已记录身份与当前规则不同 | ValueError：来源不一致；不静默清零 |
| 非法等级、负数或bool计数、非法位置、带历史但无最后窗口位置 | ValueError，点名字段 |

原六字段 JSON 带count2恢复后，新规则第一窗提前red的缺口已在固定head复核修复。**算法拒收不是数据库迁移实现**；consumer 必须决定如何向操作者报告与处理旧行，不能临时编造规则来源。

## 已收到的C2选择与待解决决策

以下归属于C2本人，来源为上面的#57回复；A记录收到，不代B2/C4/C5签字，也不把计划描述当成实现通过。

| ID | C2本人选择 | 仍需确认/证据 |
|---|---|---|
| S1 | 不回填不可验证来源；带历史legacy停机，由操作者显式重起并留审计；无历史legacy提出接受改标并记migrated_at | 候选解码缺版本一律拒收，与“无历史兼容”有差异；明确是否维持兼容承诺或修订提案，补回归 |
| S2 | 外层state_schema_version字符串"1"，state为8键；未知/未来/缺失版本分别处理；拒绝bool计数/未知键 | B2核对键名/类型；C2给JSON/SQLite往返与部分身份、未来版本拒收证据；不得直接int()静默截断非法值 |
| S3 | 默认停机+显式重起，审计放reset_event，不加表/列；C2提供函数，C3接CLI | 审计被正常写入覆盖、legacy重置入口先拒收，见R2/R4；多次重置与保留策略待评审；新CLI参数须另审 |
| S4 | last_end_seq_exclusive由已提交窗口推导；消费端校验片级/窗级连续性，拒绝重叠/空洞 | C4核对公开输入；非末片整除预检R1必须补齐；最后一片独立应用drop/error |
| S5 | features_json信封含8特征、features_version、sample_count/rate/start、规则身份和health_explain；contributions不存；created_at为落库UTC | C5确认报告可追溯性；特征版本必须与实际FEATURES_VERSION对齐，不能仅凭示例"1"自行冻结；保留规则摘要/运行身份决策 |
| S6 | 窗口/分数/告警/state/processed_chunks同片同事务；commit后采纳内存态；跳已提交数据但保留已提交state；异SHA拒收 | 23项候选测试本轮实跑通过，尚非真实kill -9；完成run分析身份冲突R3、故障恢复与并发边界仍需核实 |
| S7 | C5尚无本人回复；B2状态计数不能代替redundant/unmatched统计 | C5给独立匹配示例，B2核对状态量含义；黄/红均可命中，不增一律红级门槛 |

## 固定#64实现复核（T69）

Windows / Python3.14.6 / NumPy2.4.6，本轮固定#64源码126文件Git字节校验；消费单测23 passed。已发布 [Review5625590637](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64#pullrequestreview-5625590637)，为自动COMMENT，非人工批准。以下均为本轮公开API实跑：

| 项 | 实测结果 | 需要的修订/取舍 |
|---|---|---|
| R1/P1 非末片整除 | 512点/100点片/64点窗/drop成功，5窗且丢192点 | 结果入库前拒收非末片不整除；不是建议引入跨片缓存 |
| R2/P1 重置审计 | reset_event刚重置存在，普通状态更新后为null，重开库仍null | 必须持久保留审计；多次重置/回滚策略需决定，不能擅加公共表列 |
| R3/P2 运行身份 | 独立整除run512/128/64，首轮8窗0丢点；仅改scoring_version后4片全skip，runs仍旧版本 | 明确拒收或显式重算；同时涵盖规则内容摘要/window_points/尾窗策略，不声称已证明混合版本写入 |
| R4/P2 legacy显式重起 | 六字段旧JSON调用reset_alarm_state抛StateCompatibilityError | 正常恢复拒收与人工显式归档旧原文后重起分别定义；未来版本/损坏JSON不自动迁移 |

现行窗口只接受完整窗口；不足窗按 incomplete_window=drop 或 error，不能由本文引入“保留短尾窗”新策略。对完整窗口，start+window_points可用于end的计算，但该算式不证明跨片无空洞/无重叠，仍须用真实seq及上一提交位置检查。允许何种切窗/排序行为须由C2/C4明确，不能把“起点严格递增”当作完整连续性验证。

## 事务与重放需要证实的顺序

1. 校验 complete manifest、公开分片元数据、SHA和seq；具体责任按C4/C2接口，禁止读取private_truth或用故障配置定位窗口。
2. 在分片事务内核对 processed_chunks：已提交同part_id同SHA不再次推进分析状态；同part_id异SHA拒收。
3. 只从已提交的持久状态开始处理；窗口、分数、告警、state和processed_chunks一起提交。
4. 仅提交成功后采纳新内存状态；回滚/重启从提交态恢复，不复用未提交计算出的state。
5. 不把数据库原子提交或kill -9恢复称为已通过真实断电测试。

上述顺序是评审要求。#64已交候选事务实现及23项测试；真实kill -9、边界修复与接口收口仍待，不再称C2完全没有实现。

## 需要随接口定稿交付的证据

| 用例 | 当前证据 | 待交付 |
|---|---|---|
| 六字段/部分身份有历史拒收、空state兼容、合法同身份恢复 | #54固定head算法层专项及公开API对照 | C2实际JSON/SQLite恢复与错误出口 |
| 同分片重复消费、同ID异SHA | 未在本提案实测 | 数据库行数/状态不重复与拒收证据 |
| 跨片streak、升级/恢复、drop/error尾窗、空洞/重叠 | 未在本提案实测 | C2/B2/C4组合与位置边界测试 |
| 事务中断回滚后重放与不中断基线相同 | 未在本提案实测 | C2/C5在固定集成提交的结果对照；Linux kill -9另留证 |
| 实际告警与事件匹配/误报/重复统计 | #54自测不算独立验收 | C5独立匹配器与逐事件结果，按#56方案运行 |

## 采纳流程

B2核对源码观察及S2，C2处理本次实现差异与R1–R4，C4/C5回答其受影响部分；A已汇总C2真实答复，下面给出interface拟同步文本，收齐相关Review与兼容证据后再落interface §6/§8.4/§9。必要时分别提交文档/成员实现PR；**本草稿PR不关闭#57、不冻结字段、不替代#54独立评审或#8实现**。有异议时保留原证据并明确采用方案，不能用A单方勾选充当成员确认。

## 待审的interface同步文本（A准备，未生效）

以下是具体可审阅文本，不是已修改的interface。对应§13的相关人审查与兼容证据尚未齐备，不能据此冻结公共契约或要求成员把未定格式当基线。

### §6 消费与存储责任补充草案

生产消费链从公开样本seq传window_start_seq，禁止读取private_truth或fault_generation。consumer在业务结果提交前验证非末片rows与window_points整除；片与片首尾seq连续，完整窗口不因换片丢点或重算；仅最后一片不足窗按现行drop/error处理。消费入口调用consume_run，存储重置由C2公开函数提供；CLI解析与错误码由C3接线，业务规则与SQL不得放入CLI。

### §8.4 状态持久化补充草案

持久状态保留AlarmState八键。候选信封为state_schema_version="1"、state、reset_event；有历史必须具有可核规则身份与合法位置。不回填无法证明的来源，不把缺身份历史当fresh。缺版本、未来版本、未知键、非法类型分别报可定位错误；无历史legacy是否迁移、迁移审计字段与损坏JSON的处理仍列开放决策，不在本段静默选定。规则变化默认停止消费并提供原因；操作者显式重起应留旧原文/原因/时刻，后续正常状态更新不得删除其审计。多次重置的历史结构和CLI调用方式须评审后确定。

### §9 JSON映射与重放补充草案

现有五表与列、主外键不变；features_json拟采用特征/版本/规则身份/样本上下文与解释信封，contributions可由保存的原特征及对应规则重算，不新增特征列。告警created_at拟为落库UTC RFC3339 Z，不是信号时刻。每片特征/评分/告警/状态/processed_chunks一起提交；回滚与重启只采用已提交态。重复同片同SHA跳过分析但不丢状态，同ID异SHA拒收。结果运行身份除分片SHA外须核规则版本/内容、窗口与尾窗策略；身份冲突采用拒收或显式重算的具体方式、摘要位置及兼容策略仍待决定，不授权扩schema。

### 角色与完成条件

A+B1+B2+C4审查上述公共文本，受影响C2/C3/C5参加；C2提交修复和JSON/SQLite兼容/重放证据，C3提交CLI参数提案及接线，C5提交S5/S7与#56独立方案。A据真实意见更新interface与迁移说明，再按§13完成相关验证和独立人工Review；Linux兼容/真实kill -9未测照实标注。本文保持draft、不关闭#57；#54已经合入不是本草稿的待合前置。
