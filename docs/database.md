# 数据库设计

> 负责人：C2（@zty-78）｜Issue #8｜状态：随 #8 实现同步，`docs/interface.md` §9 仍是权威基线。
>
> 本文件不单方面修改 §9。五张业务表及 Issue #143 新增的独立审计表，均以
> `docs/interface.md` §9 和 ADR-0008 的评审版本为准；本文说明 C2 的实现、
> 两个 JSON 列的内部格式和应用级只增审计边界。
> **若 §9 与本文冲突，以 §9 为准**，并请 A 知会以同步本文。

---

## 一、五张业务表与一张审计表

§9 的表由 `src/g15/storage/database.py` 的 `SCHEMA_STATEMENTS` 原样创建。
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
| `audit_events` | `event_sequence` | 高风险操作的应用级只增审计链；不属于消费结果 |

### `audit_events`：只增与哈希链

`audit_events` 首版只记录真实 `reset-alarms`。每次记录包含 UTC、动作、
run/device/rule、脱敏来源 `local-cli`、理由、最小 payload、前驱哈希和当前哈希。
`BEFORE UPDATE` / `BEFORE DELETE` 触发器拒绝普通写连接改删历史；
`BEFORE INSERT` 守卫只允许严格的下一序号、正确尾哈希和全新事件哈希，阻断
`INSERT OR REPLACE`、序号复用、跳号与错误前驱。

仓库的 `verify_audit_chain() -> int` 按 `event_sequence` 顺序重算
SHA-256 链；成功返回已校验事件数，序号断档、前驱不一致、payload 变化或
事件哈希不符均抛 `AuditChainError`。状态 UPDATE 和审计 INSERT 位于同一个
`BEGIN IMMEDIATE` 事务，审计写失败会把状态更新一起回滚。

这不是 WORM 或外部签名日志：有文件/DDL 权限者仍可移除触发器或替换整个库。
哈希链只能使未同步重算的内部改动可检；因为没有独立尾锚，删除最后一条或整段
尾缀不可检。威胁边界、被否决方案和未来外部审计方向见
[ADR-0008](adr/0008-append-only-audit.md)。

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

### 连接的三种入口：写、读、以及「打开 ≠ 只读」

`connect()` 返回的连接**可以写**。这一点容易被忽略，因为名字里没有「写」，
但它做的两件事都是写操作：`sqlite3.connect` 在文件不存在时**创建**它，
`PRAGMA journal_mode = WAL` 会**改写数据库头**。

因此「只想看一眼这个库」这件事，**不能**用 `connect()` 干。
本模块为此另开一个入口：

| 入口 | 用途 | 是否写 | 文件不存在时 |
|---|---|---|---|
| `connect(path)` | 消费端写入 | 是（建 schema、写行） | 创建 |
| `connect_readonly(path)` | 只读观察（报表、测试轮询） | **否** | 报错，不创建 |
| `initialize_database(conn)` | 建 §9 业务表、审计表、触发器与索引 | 是 | — |

`connect_readonly()` 用 `file:...?mode=ro` URI 打开既有文件：既不创建文件，
也不改日志模式，更不与写者抢写锁。

#### 为什么只读入口是必需的（#101 实测）

Issue #101 记录了一个概率性失败：消费端建表时报
`attempt to write a readonly database`。C2 在**本机**（Windows 11）复现后做的
对照实验（每格 12 轮，记写者失败轮数）：

| 另一个进程对同一个库做的事 | 失败（C2 本机） | C4 独立复现 |
|---|---|---|
| 从不打开这个库 | 0/12 | 0/12 |
| 持**只读**连接（`mode=ro`） | 0/12 | 0/12 |
| 持**读写**连接 | 7/12 | 3/12 |
| 反复开关读写连接 | 3/12 | 2/12 |
| 只用操作系统碰这个文件（不经 SQLite） | 0/12 | 0/12 |
| 读写连接开在**别的**文件上 | 0/12 | 0/12 |

**定性方向一致**（C4 独立复现同样得到「读写句柄致失败、只读句柄不冲突」——指本组
12 轮对照的**定性方向**；完整用例时序下只读入口仍有 2/60 失败，见下节），
**量级不同**（7/12 vs 3/12），说明这是概率性现象，单次轮次不能当指标。
结论按受限环境表述：

- 在两台 Windows 11 机器上实测：**触发源是另一个进程持有该库的读写句柄**；
- 只读句柄显著降低失败率，但**未观测到归零**——这正是 `connect_readonly()`
  存在的理由；
- **「与 WAL 无关」只在本机成立**：本机把库切到 DELETE 模式反而更糟（9/15 失败），
  但文件锁语义在 Linux 与 Windows 上不同，**Linux（#10 目标环境）尚未做同款对照**，
  因此该条不写成跨平台结论。

**失败率下降，不等于归零。** C2 在本机测得观察者改用只读入口后写者 0/20、0/30
轮通过，对照组（观察者仍用读写连接）为 10/20 轮失败；但 **C4 在同一 head 上
以完整压力用例时序复测，只读入口一格仍失败 2/60（3.3%）**，另两批为 1/20
与 1/40。失败堆栈与修复前同形（写侧 `initialize_database` 的 `CREATE TABLE`）。

C4 的**同批次三格对照**（各 60 轮，同机同环境）：

| 格 | 存储侧 | 观察者形态 | 失败 | 失败率 |
|---|---|---|---|---|
| 修复前基线 | main `6f07bb43` | 读写 `connect()` | 9/60 | 15.0% |
| 仅换存储侧 | `7efd0f08` | 读写 `connect()` | 4/60 | 6.7% |
| 存储侧 + 测试侧 | `7efd0f08` | 只读 `connect_readonly()` | 2/60 | 3.3% |

⇒ **方向成立（失败率降约 1/4），但未归零。** 本节不写「只读入口 ⇒ 观察者 0 失败」。
剩余失败已定位在写侧 `initialize_database`，属另一个竞态窗口，与「只读观察者是否抢锁」
不是同一件事。#123 已把该窗口定位到**具体机制并修复**（Windows VFS 把 `-wal`/`-shm`
的读写打开静默降级为只读），详见下文「残余竞态的已证实根因与修法（#123）」；
其判定归 #101 完成条件 2，由 A 拍板。

**探针局限（如实记录）**：C4 试过的最小复现探针（父进程只做
`connect_readonly` + 查一行 + close，子进程建表）三格各 0/30 全通过，
**未能复现失败**。因此该探针**不能**支撑「只读入口不致失败」的结论；
失败依赖压力用例里更完整的时序（`shutil.copytree` 后另起子进程 + 120s
deadline 轮询）。这是探针的局限，不是「已排除」的结论。

#### 只读入口的边界

`mode=ro` 只保证**不写数据库文件**。若该库处于 WAL 模式，
SQLite 仍会创建 `-shm` / `-wal` 附属文件（WAL 的共享内存索引所需），
这是 SQLite 的设计，无法在只读模式下回避。附属文件的创建**不是**写者失败的
直接原因——上表中「持只读连接」一栏的失败率为 0/12 即是证据；
但**不能据此断言只读入口下写者永不失败**（见上节 2/60 与残余边界）。

### 残余竞态的已证实根因与修法（#123）

上节记录的「极高频开/关循环下偶发写者失败」此前**未能复现**，只能写成
「疑似 `-shm` 被清理」。#123 已把它**定位到具体机制并修复**，此处记录结论。

**根因：Windows VFS 把 `-wal` / `-shm` 的读写打开静默降级为只读。**

C4 独立复核抓到的失败现场是：主码 8、**扩展码 0**（既非
`SQLITE_READONLY_CANTINIT=776`，也非 `SQLITE_READONLY_ROLLBACK=520`），
磁盘状态 `main=4096B / -wal=0B / -shm=32768B`，`journal_mode=wal`。

扩展码 0 的**裸** `SQLITE_READONLY` 在 SQLite 3.53.4 源码中只有两个产生点：
`btreeBeginTrans` 的 `BTS_READ_ONLY` 与 `winShmMap` 的 `pShmNode->isReadonly`。
两者由同一件事驱动——附属文件的读写打开被降级：

1. Windows VFS 的 `winOpen`：`CreateFileW` 以读写方式打开失败时，**静默改用
   只读方式重开**，并把结论记在 `outFlags` 上（不报错）。
2. `sqlite3WalOpen` 把该结论缓存到 WAL 句柄（`pRet->readOnly = WAL_RDONLY`）。
3. `winOpenSharedMemory` 再缓存到**进程级** `winShmNode`。该节点按文件名缓存，
   所以同一进程内新开的连接也继承降级状态（实测 fresh connection 同样报
   `errorcode=8`）。
4. 触发源是另一进程以**拒绝写共享**的方式持有 `-wal` / `-shm`。确定性构造：
   预占位 `-wal` 或 `-shm` 后写者 **6/6 必然失败**；不碰该库的对照组 0/15，
   仅用操作系统句柄碰文件的对照组 0/15，写者不用 WAL 的对照组 0/15。

**修法：写者不再假设拿到的句柄可写。**

`connect()` 打开后用 `BEGIN IMMEDIATE` / `ROLLBACK` 探测一次（**不写任何数据**，
取写锁后立即归还）。发现句柄已降级就**关闭并重开**——重开让 SQLite 重新走一遍
自己的打开逻辑，从而**在持有者放手之后**重新决定句柄是否可写；退避负责给持有者
放手的时间。重试有界（12 次 × 50ms，约 600ms 窗口）。

> **检测是主因、重开是手段。** 退避只给持有者让出文件的时间，不是靠 sleep 掩盖。
> 探测只把 `SQLITE_READONLY` 判为降级，`SQLITE_BUSY`（锁竞争）保持原义，不误报。
>
> **重开与退避缺一不可**：只有重开没有退避，持有者不让就永远失败；只有退避没有
> 重开，原句柄不会自愈（降级确实粘在句柄上）。

**探测在 `busy_timeout = 0` 下进行（#124 修订）。**

探测用 `BEGIN IMMEDIATE` 取写锁。当**另一个连接正持有该库的写锁**时，这一句会走
SQLite 的 busy timeout（默认 5000ms）才返回 `SQLITE_BUSY`。这段等待买不到任何东西：
`SQLITE_BUSY` 恰好就是「有并发写者」，而它**不被判为降级**，所以探测等到的正是它
等不到的那把锁——实测白等 5.6 秒后照样返回一个连接。因此探测期间把
`busy_timeout` 临时置 0（探测完恢复原值），`SQLITE_BUSY` 立即返回，**检测语义完全
不变**。

| 场景 | 探测前 | 探测后 |
|---|---|---|
| 另一连接持有写锁时 `connect()` | 5.593s | **0.016s** |
| 持有 0.2s / 0.4s | 成功 0.062s / 0.282s | 成功 0.047s / 0.282s（不变） |
| 持有 0.8s / 1.2s / 2.0s | 失败 0.625s | 失败 0.609s / 0.625s / 0.610s（不变） |

**探测回滚失败时不交出连接（#124 修订）。**

若 `ROLLBACK` 失败，连接仍留在探测事务里。此时把它交给调用方，会让调用方的下一个
`BEGIN` 以「cannot start a transaction within a transaction」失败——错误点离真正
的原因太远。因此探测回滚失败会被**明确报告**：关闭该连接、保留原始错误上下文，
抛 `DatabaseError`。

**扩展码必须真的取扩展码（#124 修订）。**

`sqlite3.Error.sqlite_errorcode` 给的是**完整**结果码（主码在低字节、扩展码在高位），
不是扩展码本身。此前诊断把完整码直接当作扩展码输出，等于**丢掉了正是用来区分只读
种类的那一个数**：裸 `SQLITE_READONLY`（完整码 8 → 扩展码 **0**）会被误读成
「扩展码 8」。现按 `extended = code >> 8` 提取，本机实测：

| 情形 | `sqlite_errorcode` | primary（`& 0xFF`） | extended（`>> 8`） |
|---|---|---|---|
| 裸 `SQLITE_READONLY` | 8 | 8 | **0** |
| `SQLITE_READONLY_CANTINIT` | 776 | 8 | **3** |
| `SQLITE_CONSTRAINT_PRIMARYKEY` | 1555 | 19 | **6** |

**实测效果**（同一口径、同机、Python 3.12.15 / SQLite 3.53.4）：

| 被测 | 轮次 | 结果 |
|---|---|---|
| 修复前基线 `88f801a` | 40 | 38/40 通过，**2 失败** |
| 修复后 `6056fd7` | 60 | **60/60 通过，0 失败** |

基线失败轮的错误与本节记录的失败点逐字同型（消费子进程建表阶段因只读拒绝
退出），说明该验收口径能抓到这个 bug；修复后不再出现。

**诊断**：`describe_read_only_context()` 给出主码 / 扩展码 / 错误名、数据库
**绝对路径**、`journal_mode`、`-wal` / `-shm` / `-journal` 的存在与大小，
只读拒绝的错误消息自带这份上下文。扩展码按 `code >> 8` 取真实扩展码，
因此「裸 `SQLITE_READONLY`（扩展码 0）」与「`SQLITE_READONLY_CANTINIT`
（扩展码 3）」在诊断里分得开。

**边界（如实记录）**：降级判定基于 **Windows VFS 行为**，只在本机 Windows 11
实测；**Linux（#10 目标环境）未做同款对照**，Windows 专属回归测试在非 Windows
上自动 skip。#10 的双 Linux 真实 `kill -9` 验收独立存在，本节不替代。

两个实用建议仍然成立：

- 长驻的观察方（报表、监控）**持有连接**、在句柄上轮询，而不是每拍开关一次；
  空闲的只读连接不持锁，不妨碍写者。
- 短周期轮询方（如压力测试的 abrupt-stop 父进程）保持现有 2ms 开/关节奏
  实测安全（修复后 60/60 轮通过），但该残余风险在 Linux（#10 目标环境）上
  尚未验证，归 #10 的双 Linux 证据一并覆盖。

`connect_readonly()` 打开的是**既有**文件；文件不存在时抛
`DatabaseError`，而不是创建一个空库。这与 `connect()` 的语义相反，
是刻意的：观察一个不存在的库，正确答案是「没有」，不是「造一个空的」。

### 只读错误必须点名（#101 完成条件 4）

#### 先按错误码分流，不按措辞猜

**只有主错误码 `SQLITE_READONLY`（8）才进 `DatabaseReadOnlyError`。**
`sqlite3.Error.sqlite_errorcode` 给的是**完整**结果码：主码在低字节、扩展码在
高位（`primary = code & 0xFF`，`extended = code >> 8`）。取低字节得到主码再判断。
其余 `OperationalError` 保留自身语义、抛普通 `DatabaseError` 并带上原始上下文：

| 注入的错误 | 主码 | 抛出的异常 |
|---|---|---|
| `no such table`（SQL 写错表名） | `SQLITE_ERROR` = 1 | `DatabaseError: cannot write to the database: no such table: ...` |
| `database is locked`（他连接持写锁） | `SQLITE_BUSY` = 5 | `DatabaseError: cannot start a transaction: database is locked` |
| `attempt to write a readonly database` | `SQLITE_READONLY` = 8 | `DatabaseReadOnlyError`（见下） |

早期版本把所有 `OperationalError` 一律包装成只读错误，结果一条**纯读**语句
写错表名也会报「另一连接持有数据库」——把排障引向并发，而真因是拼写。
按错误码分流就是为了堵住这种误导。

#### 成因是「最可能」，不是判定

在主码为 8 的前提下，两种成因需要不同处置：

- **另一个连接持有该库**——竞争问题，重试或改用只读连接可以化解；
- **路径本身不可写**——权限或部署问题，重试没有意义。

本模块按**路径自身可写性**给出最可能的那一种，并明确标注这是启发式。
以下为**代码实际输出**（前缀随出错的那一步变化，`...` 表示具体路径与原始错误）：

```
cannot write to the database: <path> is not writable by this process (...)
cannot write to the database: another connection most likely holds <path> and refused
  the write -- that is the likeliest cause when the path itself is writable, not a determination (...)
```

同一条错误在不同写路径上，前缀取自那一步的动作名：
`cannot write to the database:`（事务内写）、`cannot initialise the database schema:`（建表）、
`cannot start a transaction:`（开事务）、`cannot commit the transaction:`（提交）。
路径可写性判定的那一支，句式是 `<path> is not writable by this process`——
**不是** `the path is not writable`，后者读起来像在谈论一个抽象的路径，
而实际消息里出现的是具体路径本身。

**为什么不能写成断言**：路径可写但被他进程持读写句柄时，`os.access()` 仍返回
True；而 `mode=ro` 连接在**无第二持有者**的情况下写库，同样报主码 8。
两种情形的措辞因此都留有余地——`os.access()` 只是线索，不是判定。

该错误在 `connect()`、`initialize_database()` 与 `transaction()`
三条写路径上都会抛出——事务是本模块唯一的写入口，
所以在事务里统一点名，覆盖面最完整。

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

`reason` 是操作者填写的**非空自由文本**，不设闭合枚举，实现也不做枚举校验；
`rule_change`、`legacy_unattributable`、`operator_reset` 只是文档示例，不是允许值清单。
机器可读的判断依据是 `previous_state_schema_version` 与保留的旧行原文，而不是解析这段文字。
`reset_event` / `reset_history` 的信封键名与 `state_schema_version` 均不因此改变。

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
  "features_version": "2",
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
  "health_explain": "health 99.68; drivers: mean=0.5025 (risk 0.00), rms=0.5048 (risk 0.00)",
  "risk_dimensions": {
    "level_offset": 0.0025257714598995,
    "vibration_strength": 0.0023239651275757,
    "variability": 0.0,
    "impact": 0.0245552212339881,
    "spectral": 0.703125
  }
}
```

| 内容 | 位置 | 理由 |
|---|---|---|
| 8 个特征值 | `features` | 键名与 `FEATURE_NAMES` 一致，`as_mapping()` 直接落库 |
| `FEATURES_SCHEMA_VERSION` | `features_version` | 新写为 2；读取兼容 1，旧行五维为未知，不新增列 |
| `sample_count` / `sample_rate_hz` | 独立键 | §9 表里没有这两列，但报告复算需要窗口样本数 |
| `window_start_seq` | 独立键 | 与表列 `start_seq` 冗余一致，便于单文件复原整窗上下文 |
| `scoring_version` / `rule_id` | 独立键 | **窗口级**规则身份；`runs` 表只有 run 级 `scoring_version` |
| `drivers` 与解释文本 | `health_explain` | §9 的 `feature_windows` 只有 `health_score` 列（带 `CHECK (0..100)`） |
| `HealthResult.risk_dimensions` | `risk_dimensions` | 五个现有风险分组仅作解释；组内缺特征为 `null`，不改变健康总分 |
| `HealthResult.contributions` | **不直接落库** | 原特征和五维分组分别保存；完整贡献可由对应规则复核，不在查询时换规则重算 |

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

显式 reset 使用另一笔短事务，但遵守同一原子原则：旧行 SELECT、归档、
`alarm_states` UPDATE、`audit_events` INSERT 必须全部位于同一
`BEGIN IMMEDIATE` / COMMIT 之间。若审计触发器、外键、磁盘或提交失败，状态和
审计一起回滚，不能留下“状态已清空但审计缺失”的半结果。

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

### 末片行数上界：末片不得长于名义片长（#69）

§8.1 推导末片长度是 `total_points - (n-1) * chunk_points`，这个差值**至多**是
`chunk_points`。所以末片行数有一个上界，而上界是**含等号**的：
`total_points` 恰为 `chunk_points` 整数倍时，末片行数**等于** `chunk_points`，
不是小于它（这条含等号的勘误由 #68 合入 main）。

非末片各片的 `rows` 相等（预检已要求），所以名义片长可以从非末片**推断**出来，
末片上界因此可查。消费端在预检里加一条：

```
末片 rows > 从非末片推断的 chunk_points  →  拒收
```

**用 `>` 判定，不用 `>=`。** 用 `>=` 会把合法的等长整除末片误拒——
那是每个 `total_points` 为 `chunk_points` 整数倍的 run 都会踩的坑。

这条校验放在**业务写入之前**，所以拒收时 `runs` / `feature_windows` /
`alarms` / `processed_chunks` 一行都不留（空库与空表允许存在）。

| 输入 | 结果 |
|---|---|
| 非末片 128、末片 256，窗 64 | 拒绝：末片 256 > 推断的 128，点名 part 1 / rows / 上界 |
| 非末片 128、末片 128，窗 64 | 通过，4 窗，0 丢点（等长整除末片必须放行） |
| 非末片 128、末片 100，窗 64 | 通过，3 窗，丢 36 点（合法短尾，按 drop） |
| 单片 512，窗 64 | 通过，8 窗（单片无从推断名义片长，不反推） |

**单片为什么不反推**：manifest 不携带 `chunk_points`，单片时没有别的片可供参考，
凭空造一个名义值去卡它，等于用一个不存在的事实拒绝合法 run。§4 把这一情形
留给调用方的配置配对责任。

**报错先于整除报错**：非末片 100（不是 64 的倍数）且末片 200 时，两个规则都违反，
但错误点名**末片**——长度越界说明这个 run 根本没按清单声称的方式切，比整除更根本。

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
