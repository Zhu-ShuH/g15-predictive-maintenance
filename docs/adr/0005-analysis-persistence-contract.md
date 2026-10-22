# ADR-0005：分析结果与告警恢复的候选持久化契约

- 状态：**提案，未冻结**；2026-10-22；关联 [Issue #57](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57)、#54、#8、#2。
- 准备：A/Codex，按组长“完成下一轮待办”整理；本文不代表 C2/C4/C5 已确认。
- 权威基线仍为 [interface](../interface.md)、[acceptance](../acceptance.md) 与 [ADR-0004](0004-recovery-and-independent-evaluation.md)。本文供逐项评审，不覆盖基线、不授权提前按候选字段建表。

## 审阅对象与当前事实

本轮 main 固定为 `c60f3904567aa5f0b637494abb231de597f5ab37`；候选算法为 [PR #54](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/54) 的 `da3a52b9105edfd0c418c7c29369fd6c68aab01d`。main 的 analysis 与 consumer 仍占位，不能把分支实现或本提案算作已落地主链。

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
| Alarm | 不含 run_id/device_id/created_at | consumer 从 manifest 附加 run/device，created_at 来源待 C2说明；唯一键保持现行 run/device/rule/start，不能把规则身份或窗口位置丢掉 |
| AlarmState | 六个状态量加 scoring_version/rule_id；带历史时身份和位置必需 | state_json 应能完整恢复8字段；外层 state_schema_version、编码与迁移方案待 C2，不在本文冒称已有schema或冻结格式 |

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

## C2/B2必须给出具体选择的决策卡

下面全部是开放问题，不预选、不代签：

| ID | 需要本人确认的内容 | 责任/所需证据 |
|---|---|---|
| S1 | 六字段或部分身份旧行：有可验证来源时回填，或显式重起streak并留审计；无来源不可伪造身份 | C2+B2给选择、失败流程与迁移/兼容用例 |
| S2 | state_json版本放在外层还是其他明确位置，如何识别未知/缺失/未来版本；8字段如何编码/恢复 | C2给示例与拒收/兼容策略，B2核对类型 |
| S3 | 改规则导致拒收：停机等人工处理，还是显式归档后重起；如何留审计、如何恢复任务 | C2给操作与事务边界，B2核对状态语义 |
| S4 | last_end_seq_exclusive与last_window_start_seq的关系，跨片连续性/重叠/空洞如何检测 | C2+C4给处理顺序和边界用例 |
| S5 | features_json、评分解释与特征/规则版本的追溯映射，以及告警created_at来源 | C2+C5给字段位置/来源，不扩散到未经批准的schema |
| S6 | 同事务提交与回滚后内存态恢复；同part_id异SHA拒收；已提交同SHA分片跳过 | C2给重放/故障测试证据，C4核对公开数据输入 |
| S7 | redundant_alarm_count、unmatched_alarm_count与实际告警/事件匹配；不使用alarm_windows代替统计 | C5给映射示例，B2核对状态量含义 |

现行窗口只接受完整窗口；不足窗按 incomplete_window=drop 或 error，不能由本文引入“保留短尾窗”新策略。对完整窗口，start+window_points可用于end的计算，但该算式不证明跨片无空洞/无重叠，仍须用真实seq及上一提交位置检查。允许何种切窗/排序行为须由C2/C4明确，不能把“起点严格递增”当作完整连续性验证。

## 事务与重放需要证实的顺序

1. 校验 complete manifest、公开分片元数据、SHA和seq；具体责任按C4/C2接口，禁止读取private_truth或用故障配置定位窗口。
2. 在分片事务内核对 processed_chunks：已提交同part_id同SHA不再次推进分析状态；同part_id异SHA拒收。
3. 只从已提交的持久状态开始处理；窗口、分数、告警、state和processed_chunks一起提交。
4. 仅提交成功后采纳新内存状态；回滚/重启从提交态恢复，不复用未提交计算出的state。
5. 不把数据库原子提交或kill -9恢复称为已通过真实断电测试。

上面是基线约束的待实现/待验证整理，不是C2已实现的事务代码。

## 需要随接口定稿交付的证据

| 用例 | 当前证据 | 待交付 |
|---|---|---|
| 六字段/部分身份有历史拒收、空state兼容、合法同身份恢复 | #54固定head算法层专项及公开API对照 | C2实际JSON/SQLite恢复与错误出口 |
| 同分片重复消费、同ID异SHA | 未在本提案实测 | 数据库行数/状态不重复与拒收证据 |
| 跨片streak、升级/恢复、drop/error尾窗、空洞/重叠 | 未在本提案实测 | C2/B2/C4组合与位置边界测试 |
| 事务中断回滚后重放与不中断基线相同 | 未在本提案实测 | C2/C5在固定集成提交的结果对照；Linux kill -9另留证 |
| 实际告警与事件匹配/误报/重复统计 | #54自测不算独立验收 | C5独立匹配器与逐事件结果，按#56方案运行 |

## 采纳流程

B2核对源码观察；C2逐项回答S1–S6，C4/C5回答其受影响部分；A汇总真实意见，更新本提案/迁移说明，再同步interface §6/§8.4/§9并按§13走相关Review与兼容测试。必要时分别提交文档/成员实现PR；**本草稿PR不关闭#57、不冻结字段、不替代#54独立评审或#8实现**。有异议时保留原证据并明确采用方案，不能用A单方勾选充当成员确认。
