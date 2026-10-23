# 数据库设计

> 负责人：C2（@zty-78）｜Issue #8｜状态：随 #8 实现同步，`docs/interface.md` §9 仍是权威基线。
>
> 本文件**不新增也不修改** §9 的表结构。§9 的五张表、字段与主键以 A 同步后的版本为准；
> 本文只说明 C2 的实现如何落进那些表，以及两个 JSON 列的内部格式。
> **若 §9 与本文冲突，以 §9 为准**，并请 A 知会以同步本文。

---

## 一、五张表

§9 的五张表由 `src/g15/storage/database.py` 的 `SCHEMA_STATEMENTS` 原样创建。
字段不增不减，唯一添加的是两个索引（`feature_windows(run_id, start_seq)` 与
`alarms(run_id, start_seq)`），用于报告侧按位置查询；§9 允许核心表扩展字段与索引，
且这两个索引不改变任何主键或外键语义。

| 表 | 主键 | 作用 |
|---|---|---|
| `runs` | `run_id` | 一次运行的元数据（场景、schema 版本、规则版本、状态、开始时刻） |
| `processed_chunks` | `(run_id, part_id)` | 已处理分片与其 SHA-256，幂等与重放的依据 |
| `feature_windows` | `(run_id, device_id, start_seq)` | 每个窗口的特征与健康分 |
| `alarms` | `(run_id, device_id, rule_id, start_seq)` | 告警行 |
| `alarm_states` | `(run_id, device_id, rule_id)` | 跨分片的告警状态 |

### `runs.status` 的语义：manifest 快照，**不是**「已消费完」

`runs.status` 原样镜像 manifest 的 `status`，写入后不再更新。它回答的是
「这份 manifest 当初发布成什么状态」，**不回答**「这个 run 有没有被消费完」。
语义上不承载两个意思，是刻意的：

| 想知道 | 看哪里 |
|---|---|
| manifest 是否 `complete` | `runs.status` |
| 是否已消费完 | `COUNT(processed_chunks WHERE run_id=?)` 是否等于 `len(manifest.parts)` |
| 消费到哪个位置 | `alarm_states.last_end_seq_exclusive` |

所以「`runs.status='complete'` 但只写了 3 片」不是矛盾数据，而是中途失败的正常痕迹：
判据是 `processed_chunks` 的行数，不是 `status`。§9 没有为「消费完成」单列字段，
本实现**不新增列**，因此不额外写完成标记。

### `runs` 行是运行身份，不是标签

同一个 `run_id` 再次被消费时，`scenario_id` / `schema_version` / `scoring_version` /
`status` / `started_at` 必须与已存行**逐字段一致**，任一不同即抛 `RunConflictError`
（退出码 3）。理由与「同 `part_id` 异 SHA 拒收」同源：库里已有的窗口是按另一套规则
算出来的，静默沿用旧行等于把两次分析混进同一个 run。想换规则重算，走新数据库或显式重置。

### 连接级 PRAGMA

| PRAGMA | 值 | 理由 |
|---|---|---|
| `foreign_keys` | `ON` | SQLite 默认关闭；§9 的外键引用必须真正生效。开启后**读回校验**，不是设了就算 |
| `journal_mode` | `WAL` | 读写不互相阻塞，且进程被杀时未提交事务可回滚。同样读回校验 |
| `synchronous` | `FULL` | WAL 下默认的 `NORMAL` 在断电时可能丢已提交事务；§9 要求 kill -9 后可重放，取更保守的一档 |

`foreign_keys` 在事务内设置是**无效操作**，所以连接建立后、任何写入前就设好。

---

## 二、两个 JSON 列的内部格式

§9 把 `features_json` 与 `state_json` 定义为 `TEXT`，内部格式由 C2 定。
两者用同一套原则：**信封结构**，把「格式版本」「内容」「审计」三件事分开，
不让 C2 的私有信息污染 B2 定义的字段契约。

### 2.1 `alarm_states.state_json`

```json
{
  "state_schema_version": "1",
  "state": {
    "active_level": "yellow",
    "consecutive_breaches": 0,
    "normal_windows": 3,
    "alarm_windows": 1,
    "last_window_start_seq": 4096,
    "recovered_at_seq": null,
    "scoring_version": "heuristic-v1",
    "rule_id": "health_heuristic"
  },
  "reset_event": null
}
```

| 段 | 内容 | 归属 |
|---|---|---|
| `state_schema_version` | 信封格式版本，当前 `"1"` | C2 |
| `state` | B2 `AlarmState` 的**八个**字段，键名与 dataclass 字段**完全同名** | B2 |
| `reset_event` | 显式重置的审计记录 | C2 |

`state` 段**恰好**是那八个键：多一个键、少一个键，都会被拒绝并点名。
静默丢弃未知键会让「字段名写错了」的行看起来合法。

#### 版本识别

| 读到的情况 | 判定 | 动作 |
|---|---|---|
| 无 `state_schema_version` | legacy（八字段时代之前） | 拒绝并说明来源无法确定；带历史的行需显式重起，无历史的行可接受并改标 |
| `"1"` | 当前版本 | 按 v1 读 |
| 其它任何值 | 未知/未来版本 | **拒绝**，不猜测、不按 v0 读、不忽略字段 |

未来版本必须拒绝的原因：若有人给 `state` 加了第九个字段并升版本，
旧 consumer 按 v1 读会**静默丢掉第九个字段**，恢复出一个不完整但看起来合法的状态。
版本不匹配必须炸。

#### 编码硬约束

| 字段 | JSON 形态 | 约束 |
|---|---|---|
| `active_level` | `null` / `"yellow"` / `"red"` | 其它值拒绝 |
| `consecutive_breaches` / `normal_windows` / `alarm_windows` | JSON number | **必须是整数**；`2.0`、`true`、`"2"` 一律拒绝 |
| `last_window_start_seq` / `recovered_at_seq` | JSON number 或 `null` | 整数或 null |
| `scoring_version` / `rule_id` | 字符串或 `null` | 字符串或 null |

**落库前必须 `int()` 转换。** Python 的 `json.dumps` 会把 `int` 写成 `2`、把 `float` 写成 `2.0`；
恢复后 `isinstance(2.0, int)` 为 `False`，B2 的严格类型校验会直接拒收。
同理**禁止** `bool`（Python 里 `isinstance(True, int)` 为 `True`）与 NumPy 标量
（`json.dumps(np.int64(2))` 会抛 `TypeError`）。

#### `reset_event`

显式重置后非空，保留被重置前的原文，供审计：

```json
"reset_event": {
  "reason": "rule_change",
  "reset_at_utc": "2026-10-23T05:00:00Z",
  "previous_state_schema_version": "1",
  "previous_state": { "...重置前的八字段原文（能解码时）..." },
  "previous_state_json": "{...重置前的整段原文，逐字节保留...}"
}
```

`reason` 取三个值：`rule_change`（规则身份不兼容）、`legacy_unattributable`
（legacy 且来源不可考）、`operator_reset`（人工主动重置）。

**审计必须活过后续写入。** 普通窗口写 `state_json` 时编码器并不知道历史，
若整段覆盖，重置记录会在下一个窗口就消失——重置确实发生了，但库里查不到。
因此 `upsert_alarm_state()` 默认把库里已有的 `reset_event` / `reset_history`
原样带回新信封（`preserve_reset_event=True`）。这条与 §57 S1/S3 的承诺对齐：
**重置是显式动作，且事后可查。**

**多次重置不互相覆盖。** 第二次重置时，上一次的 `reset_event` 会被推进
`reset_history`（数组，最旧在前），新的记录留在 `reset_event`：

```json
"reset_event":   { "...最近一次重置..." },
"reset_history": [ { "...更早的一次重置..." } ]
```

`reset_history` 只在至少重置过一次时出现，没重置过的行不带空数组。

#### 显式重置两条路径的分工

| 路径 | 对旧行的态度 | 理由 |
|---|---|---|
| 正常消费（`get_alarm_state` → `decode_state_json`） | **严格**：版本未知、缺字段、类型不对一律拒收 | 读不懂的行不能当读懂了用 |
| 操作者显式重置（`reset_alarm_state`） | **宽容**：不要求读懂，原文归档后重起 | 逃生门若只在「能读懂」时可用，那它永远用不上 |

因此 legacy 六字段行、未知未来版本、甚至损坏的 JSON，`reset_alarm_state()`
都能执行：旧原文进 `previous_state_json`，`reason` 记 `legacy_unattributable` 或
`operator_reset`，新 streak 的 `scoring_version` 取参数或旧行里能读到的值。
`allow_legacy=False` 可关闭这条宽容路径，恢复「必须可解码才能重置」。
**注意：宽容只针对操作者显式重置，正常消费的严格拒收没有任何放松。**

### 2.2 `feature_windows.features_json`

```json
{
  "features_version": "1",
  "features": {
    "mean": 0.5025257714598995,
    "std": 0.048115327971037396,
    "rms": 0.5048239651275757,
    "ptp": 0.27935618012735014,
    "kurtosis_excess": 0.07366587770196542,
    "crest_factor": 1.289571479380397,
    "dominant_freq_hz": 7.421875,
    "zero_crossing_rate": 0.4549019607843137
  },
  "sample_count": 256,
  "sample_rate_hz": 100,
  "window_start_seq": 0,
  "scoring_version": "heuristic-v1",
  "rule_id": "health_heuristic",
  "health_explain": "health 99.68; drivers: mean=0.5025 (risk 0.00), rms=0.5048 (risk 0.00)"
}
```

| 内容 | 位置 | 理由 |
|---|---|---|
| 8 个特征值 | `features` | 键名与 `FEATURE_NAMES` 一致，`as_mapping()` 直接落库 |
| `FEATURES_VERSION` | `features_version` | 每行的版本追溯，不新增列 |
| `sample_count` / `sample_rate_hz` | 独立键 | §9 表里没有这两列，但报告复算需要窗口样本数 |
| `window_start_seq` | 独立键 | 与表列 `start_seq` 冗余一致，便于单文件复原整窗上下文 |
| `scoring_version` / `rule_id` | 独立键 | **窗口级**规则身份；`runs` 表只有 run 级 `scoring_version` |
| `drivers` 与解释文本 | `health_explain` | §9 的 `feature_windows` 只有 `health_score` 列（带 `CHECK (0..100)`） |
| `HealthResult.contributions` | **不落库** | 8 个特征的完整分解，可由 `features` + 规则文件重算，避免 JSON 膨胀 |

---

## 三、事务边界：一片一个事务

`src/g15/storage/database.py` 的 `transaction()` 是**唯一**的写入路径。
一个分片的事务里包含：窗口特征、健康分、告警、`alarm_states`、`processed_chunks`。

```
读 manifest（只认 status=complete）
  → 读片（read_chunk 传 run_dir=，强归属校验）
    → 纯计算阶段：特征 / 评分 / 告警全部算完
        ├─ 任何 ValueError → 一行不写、事务不启，退出码 3
        └─ 全部通过 → 开事务
             ├─ feature_windows / alarms / alarm_states / processed_chunks 一起写
             └─ COMMIT 成功 → 才把新的 AlarmState 采纳为内存态
```

**「提交成功才采纳内存态」是回滚后不丢数据的关键**：commit 失败或进程被杀时，
内存里的新状态一并作废，下次从库里已提交的 `state_json` 重建。

消费启动时，初始状态**永远来自库**（`alarm_states`），不来自任何进程内残留。

---

## 四、幂等与拒绝

### 幂等

`processed_chunks` 主键 `(run_id, part_id)`：

| 情况 | 动作 |
|---|---|
| 该 part_id 未处理过 | 正常分析并写入 |
| 该 part_id 已处理，SHA 相同 | **跳过该片的分析计算**；但仍从库里读回 state 继续 |
| 该 part_id 已处理，SHA 不同 | **拒绝**，抛契约冲突错误，退出码 3 |

**「跳过数据」不等于「跳过状态」**：跳过一片时若不载入 state，
streak 会在分片边界断掉，`consecutive_breaches` 归零，告警去抖逻辑失效。
这是实现中最容易写错的一处。

### 拒绝而非猜测

| 情况 | 行为 |
|---|---|
| manifest `status != complete` | 拒绝，退出码 3 |
| 分片 SHA-256 不符 | 拒绝入库并记日志（`ChecksumMismatchError`） |
| 分片字段/dtype/行数不符 | 拒绝入库并记日志（`ChunkValidationError`） |
| 空分片 | 拒绝 |
| 片间 seq 有空洞或重叠 | 拒绝 |
| 窗间 seq 有空洞或重叠 | 拒绝（`next_start != prev_start + window_points`） |
| 续跑位置与已提交窗口末端不符 | 拒绝 |
| 全新消费时首片起点不是 `seq=0` | 拒绝（片内平移会让整条时间轴的窗口位置整体偏移） |
| `state_json` 版本未知/缺失 | 拒绝（见 §2.1） |
| 非末尾分片 `rows % window_points != 0` | **入库前拒绝**（见下） |
| 非末尾分片长度彼此不等 | **入库前拒绝** |
| 同 `run_id` 但 `scenario_id`/`scoring_version`/`started_at` 等变化 | 拒绝（`RunConflictError`） |
| 库内已有窗口的宽度与本次 `window_points` 不同 | 拒绝 |

`read_chunk` 不校验分片归属与跨片连续性，这两件事都在 C2 侧；
`read_chunk` 也不校验窗口级连续性，同样由 C2 负责。

### 窗口栅格预检：先验，再写

§8.1 要求「非末尾分片必须是 `window_points` 的整数倍」。manifest 里没有
`chunk_points`，只有每片的 `rows`，所以预检这么推断：**非末尾各片的 `rows` 必然相等，
取该值当 `chunk_points`**，再要求它整除 `window_points`。

为什么必须在**任何写入之前**做：如果按片各自取模，`window_points` 不整除片长时
**每一片**都会丢掉尾部余数，窗口起点也不再落在全局栅格上，而 `consume_run`
仍然返回成功。极端情形（`window_points` 大于片长）会把 100% 样本丢掉，
库里却是「一个消费成功的 run，零特征零告警」。这是静默伪造数据，
与 §8.1 冲突，因此改为拒收，错误信息点名 `part_id` / `rows` / `window_points`。

预检放在 `ensure_run()` 之前，所以非法输入**连 `runs` 行都不会留下**。

只有末片允许有余数：`drop` 计入 `windows_dropped`（注意该字段计的是**样本数**，
名字沿用已评审的接口），`error` 则整体拒收。

| 输入 | 结果 |
|---|---|
| 5×100 + 12 片，`window_points=64` | 拒绝：100 不是 64 的倍数，一行不写 |
| 4×128 片，`window_points=64` | 通过，8 窗，0 丢点 |
| 7×10240 + 4096 片，`window_points=256` | 通过，末片 4096 整除 256，0 丢点 |
| 末片 100 点，`window_points=64`，`drop` | 通过，1 窗，丢 36 点 |
| 同上但 `error` | 拒绝 |

### 「分析身份」一致：不只验分片 SHA

只验 `processed_chunks.sha256` 只能证明**输入**没变，不能证明**分析**没变。
所以消费入口另外核对三件事，任一不符即拒收：

| 身份要素 | 依据 | 检不出来会怎样 |
|---|---|---|
| `scoring_version` / `rule_id` | `runs` 行 + `alarm_states.state_json` | 规则换了却当同配置重放，库内新旧规则窗口混排 |
| `window_points` | 库里已有窗口的 `end_seq_exclusive - start_seq` | 同一 run 里混入两种宽度的窗口，报告复算全错 |
| `incomplete_window` 策略 | §9 无列可记（见下） | 只影响末窗有无；换策略须显式重置 |

`window_points` 没有独立列，但窗口几何本身就带着它，读回第一行即可比对——
**不新增列**也做得到。`incomplete_window` 在库里没有痕迹（尾窗要么存在、要么不存在），
因此它的变更只能由操作者显式重置来声明；这一点在 #57 的答复里写明了边界，
没有把它说成「已自动检测」。

### 连续性的两个层次

```
片级：chunk.seq[0] == 上一片 chunk.seq[-1] + 1
窗级：下一窗 start_seq == 上一窗 start_seq + window_points
```

只靠 `start_seq` 严格递增查不出空洞与重叠——递增能挡住「往回走」，
挡不住「跳过去」或「叠上来」。

---

## 五、`alarms.created_at` 的来源

**消费落库时刻的 UTC 时间**，RFC 3339 带 `Z`，与 `processed_chunks.processed_at` 同源同时钟。

它**不是**数据的绝对位置（那是 `start_seq` 的职责），**不是** run 的
`start_time_utc`，也**不是**故障发生时刻（那是 `private_truth` 的事，消费者禁止读取）。
它的用途是审计与运维排障。

---

## 六、消费者绝不做的三件事

1. **不读 `private_truth/`** —— 消费者、特征、健康评分、告警程序都不得读取
   `runs/<run_id>/private_truth/faults.json`。
2. **不读生产者场景配置** —— `ConsumerConfig` 只有 `window_points`、
   `incomplete_window`、`health_rules_file`，不含任何故障位置。
3. **不自己开 npz 文件** —— 一律走 `read_chunk`，因此 `allow_pickle=False` 不会被绕过。

---

## 七、迁移说明

`alarm_states` 表从未在任何环境创建过，`origin/main` 上 C2 的三个模块均为 0 字节，
所以**当前没有存量行需要迁移**。

从本版起，写入的 `state_json` 一律是 v1 信封。若将来再次演进：

| 情形 | 处置 |
|---|---|
| 新增 `state` 字段 | 升 `state_schema_version`，旧 consumer 读到新版本 → 拒绝（不静默丢字段） |
| 旧 v0 行（无版本号） | 带历史 → 拒绝并要求显式重起；无历史 → 接受并改标 + 记 `migrated_at` |
| 规则身份不兼容 | 消费停机（退出码 3），由操作者显式重起；重起前的原文留在 `reset_event.previous_state` / `previous_state_json` |
| legacy 行需要重起 | `reset_alarm_state()` 不要求可解码：原文归档、新 streak 起于空态（见 §2.1） |

**不回填身份。** 要回填 `scoring_version` / `rule_id`，前提是能证明该行由哪套规则产生；
而旧行缺失身份字段恰恰说明写它时没有身份可记。回填等于用无从考证的值去补一个
要求「可验证」的字段，并让旧规则的计数在新规则下复活——这与 A 在 #54 的 P1
要根治的是同一类「静默继承」，只是方向相反。
