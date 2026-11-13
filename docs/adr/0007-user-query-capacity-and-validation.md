# ADR-0007：用户健康查询入口、十万级口径与容量验证

- 状态（2026-11-11，main `a9951403`）：**#137 契约已合入，#135 查询服务已由 #149 实现；十万不同用户同时真实查询的主目标已定；#136 容量门禁未执行，同步/统计/SLO/资源仍未冻结，未执行高负载**
- 关联：[Issue #133](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/133)、[#135](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/135)、#136、#2、#9、#56
- 历史送审基线：main `6437130ab4898921515ca2cec3a5db25c8e234ca`；#137 已合入为 `ca328f49`，查询实现 #149 已合入为 `a9e35e68`
- 来源边界：组长基于课堂侧重点追加的工程工作假设，不是教师正式评分细则或权重
- 负责人：A 与 B1；必要非作者/公共契约 Review：C4；最终门禁：A（不自批）

## 1. 历史背景与当前实现

在上述送审基线，G15 是以 CLI 驱动离线生产、消费、SQLite、完整性校验和报告的模块化单体，当时没有 HTTP/ASGI 服务、认证授权或请求级追踪。#149 已新增标准库 HTTP 查询入口、HMAC bearer 校验、静态授权、只读 SQLite、稳定分页与脱敏追踪；这只证明查询实现已交付，不代表 #136 Gate 0、D0 规模验收、D1/D2、生产登录或十万级容量已通过。

下文保留容量提案与 #137 送审时的候选措辞；“候选/待复核”描述当时的方案阶段，不表示 #137/#149 仍未合入，也不把全部容量门槛视为已冻结。具体契约与实现以 #137/#149 的合入内容和正式 Review 为依据，容量执行仍按 #136 单独取证。

现有 `connect_readonly()` 以 SQLite URI `mode=ro` 打开每个 `run_dir/artifacts/results.sqlite`，`feature_windows(run_id,start_seq)` 和 `alarms(run_id,start_seq)` 有索引。这些是查询底座，不是并发容量证明。WAL 只说明读者通常不阻塞单写者；百万点生产、三进程消费竞态和中断恢复测试都不是用户查询压力测试。

本 ADR 不复制或披露外部课堂参考项目内容。涉及第三方的信息和知识产权不得进入公开仓库、测试数据或日志。

## 2. 增量目标与非目标

目标是先定义一条真实、可校验的用户查询路径，再逐级取得容量证据：

> 经认证且获授权的操作员，按可见的 `run_id + device_id` 查询最近一页健康窗口及告警；响应来自已提交的 `runs / feature_windows / alarms / alarm_states`，具有稳定顺序/游标、页大小、数据版本、健康分、告警级别/原因、窗口序号和 `scoring_version/rule_id`，且不读取 `private_truth`。

组长已经确定主目标为：100,000 个不同且已认证、获授权的用户在同一受控波次中发起上述真实查询，并以客户端与服务端实际在途状态证明“同时查询”。Review 只精确化同步窗口、统计区间、状态边界、SLO、资源和拓扑，不再选择 H 或 F。

非目标：

- 不以 CLI、HTTP 200、空 handler、常量响应、`sleep` 或 mock 数据库冒充业务查询；
- 不把百万点、请求总数、连接数、VU 或 WAL 能力改名为“100k 同时查询”；
- 不在本 ADR 选择 Web 框架、购买/租用资源、执行十万级负载或合并实现；
- 不改变 #56/#9 的“计划与选择方法先审 → 冻结 → 首次执行”顺序；
- 不改变现有成员模块属地；新增工作由 A/B1 承接，跨入既有模块须另行授权和 Review。

## 3. 已决定的主目标与分阶段量测

主场景采用 F：100,000 个不同用户在同一受控波次中各发起一条真实主请求。每个端点的阶段使用本进程单调时钟，跨端用同一 request/trace ID 关联并按匿名化用户去重；跨进程时间不得直接相减：

| 字段 | 定义 |
|---|---|
| `client_distinct_users` | 本波次发起主请求的不同已认证/授权用户数 |
| `client_started` | 越过发送 barrier 并开始发送应用请求；建连或等待 barrier 不计 |
| `client_waiting` | 主请求已完整发送、尚未收到完整响应或终态错误 |
| `client_completed` | 客户端已完整收到响应或终态错误 |
| `server_received` | 入口已收到完整请求并分配 request/trace ID |
| `server_queued` | 已接收但真实业务执行尚未开始 |
| `server_executing` | 认证/授权与 handler → repository → SQLite 正在执行 |
| `server_completed` | 终态响应已完整交付传输层；客户端接收另记 |
| `client_in_flight` | 同一时刻 `client_started - client_completed` 的不同用户主请求数 |
| `server_in_flight` | 同一时刻 `server_received - server_completed` 的不同用户主请求数，包含排队、执行及响应交付中的请求 |
| `total_requests` | 累计尝试/完成数；重试单列，不能补足不同用户数 |
| `arrival_rate_rps` | 开放式模型每秒发起率；关闭式 VU 不得冒充固定到达率 |
| `open_connections` | 同一时刻传输连接数，不等于查询或用户数 |
| `active_virtual_users` | 客户端状态机数，不等于实际在途 |
| `success_throughput_rps` | 业务与授权均正确的完成吞吐 |

分别报告客户端与服务端的不同用户在途峰值、发生时刻、排队/执行组成及采样方法。只有某阶段的不同用户峰值真实达到 100,000，才能声称该阶段达到 100k；同步发起不能自动推导服务端已达到同样峰值。重试、重定向、重复请求、连接数和累计请求均不能补足用户数。具体同步窗口、统计区间、时钟误差和最终判定阶段由 Review 精确冻结。

`syc` 为 `src` 的笔误；`test` 与 `src` 的目录对应关系按 B1 复核记录，不再作为待定术语。

### 3.1 #157 阶段观测与分进程量测候选（2026-11-12）

A 继续修复 #157 并补全 Gate 0，候选实现及精确事件边界见 interface §16。新增 `server_executing` 与 `server_execution_completed` 配对，覆盖 framing/认证授权/SQLite 的执行区间，结束点在响应序列化与发送之前；Expect/非 GET 用 execution_not_started。每事件原发纳秒单调时钟和 PID 随证据保留，只在同一个服务端进程内计算执行/接收在途，不能按跨进程事件到达时刻计数。

当前没有应用任务队列，非阻塞 worker 满额即拒绝；queued=null，application_queue=absent，listen_backlog=unmeasured。queued 是否记为验收 N/A 仍待 B1/C4 复核与 A 明确裁定，不能因工具写了 absent 自动通过该项。

`process_runner` 分离客户端/服务端进程。并发主波次预先建立真实 TCP 短连接，再由客户端发送前 barrier 同步起跑；建连与 barrier 等待单列，client_started 在真实请求发送前记录。请求使用已冻结上限内的 page_size=100，真实读取 D0 的100个窗口。服务端没有 barrier、sleep、人工延迟或限速。各进程资源分别采样，CPU按该进程的单调区间求差，RSS/FD/handle采样最大值仅为已观察下界；实际峰值及目标未达必须原样报告。

本增量保持 Gate 0 / D0 / 10与50边界，不授权D1/D2或更高负载。事件、资源与工作负载口径属于本PR候选，真实非作者评审和同SHA双Linux证据仍是完整Gate0前置。

## 4. 主场景 F 与补充场景 H

### 主场景 F：十万不同用户同时真实查询

- 100,000 个不同身份在待审同步窗口内各越过发送 barrier，并各发起一条真实主请求；
- 客户端发起/等待和服务端接收/排队/执行/完成必须逐阶段实测、按用户去重并关联同一 request/trace ID；
- barrier 只能控制发送前起跑；禁止在 handler 中 `sleep`、人工变慢业务、压住/延后响应、故意扩大队列或用网络限速制造在途峰值；
- 达不到目标时如实失败，不能退回 H 作为替代通过；
- 同步窗口、观测/统计区间、采样精度、SLO、资源与分布式负载拓扑仍待 Review；
- 若用 5 秒观察 100,000 个请求完成，平均至少约 20,000 query/s 才能清空；这是容量算术，不是冻结 SLO 或能力证据。

候选交互目标供 Review：热态 p50≤200ms、p95≤500ms、p99≤1s，客户端 timeout 2 秒，5xx+timeout<0.1%；错误、越权与跨权限缓存泄漏为 0；冷启动首分钟单列、候选 p95≤2s。这些数字不是教师要求，也尚未冻结。

### 补充场景 H：带 think time 的持续活跃用户

H 只用于常态交互、稳定段和 soak，不替代 F。关闭式 VU 的稳态吞吐近似：

```text
X_closed ≈ N / (think_time + mean_response_time)
```

当 `N=100,000`、think time=30 秒且平均响应时间趋近于 0 时，3,333.3 req/s 只是近似上限；响应时间为正时吞吐更低。需要固定到达率时必须使用开放式模型，并分别报告计划/实际到达率、生成端丢弃与饱和。H 的任何结果均不得降低、替代或宣告 F 通过。

## 5. 数据、认证、正确性与缓存

现行 `benchmark_1m.yaml` 为 1,000,000 点、`chunk_points=102,400`，`consumer.yaml` 为 `window_points=256`、`incomplete_window=drop`。9 个完整分片各有 400 窗，末片 78,400 点有 306 个完整窗并丢 64 点，因此每 run 为 `floor(1,000,000 / 256) = 3,906` 个完整窗口。候选容量数据：

- D0：10 设备 × 100 窗口，用于正确性；
- D1：1,000 设备 × 3,906 窗口，共 3,906,000（390.6 万）行，按现行布局分散在 1,000 个 run 数据库；
- D2：10,000 设备 × 3,906 窗口，共 39,060,000（3,906 万）行，按现行布局分散在 10,000 个 run 数据库；候选为 10 用户/设备授权。

现行 Manifest 契约是一 run 一 device，且每个 run 使用独立 `results.sqlite`；因此 D1/D2 是跨 1,000/10,000 个数据库汇总的 `feature_windows` 规模，每库每 run 约 3,906 行，告警、状态与历史 run 另计。历史 run 会继续增加数据库数量。若改成共享读模型或单库多设备布局，须另审 Schema、迁移、目录/聚合策略和代表性查询计划。

用户/设备比、历史 run、告警密度、响应大小、保留周期和分页上限未冻结。D2 在磁盘、建库时间、路由成本和 RSS 未测前不得称为可行。

实现前必须冻结：

1. 身份来源及 user—角色/租户—device—run 授权映射；
2. 401/403/404 是否隐藏资源存在性；
3. 独立 SQL/oracle 与每个响应的正确性对账；
4. 稳定分页：冻结查询范围、确定性全序、游标字段、页大小、方向及翻页期间已提交数据的可见性；窗口可评审 `start_seq`，告警须用 `start_seq + rule_id` 或等价唯一键打破并列，不以仅 OFFSET 冒充稳定游标；
5. 具名版本：区分 Manifest `schema_version=2.0`、配置契约 `SCHEMA_VERSION=1.0`、特征信封 `features_version`、状态信封 `state_schema_version`，并保持 `scoring_version` / `rule_id` 独立；没有持久化来源的配置版本不得推断或返回；
6. 提交态新鲜度：只依据只读事务可见的已提交行、序号、状态或持久化时间，不读 `private_truth`、不以真值注入时间推导；跨库查询记录各库 `as_of`，未实现共享快照前不宣称全局原子新鲜度；
7. 响应投影：普通健康查询只按允许列表返回业务 `state` 与获准版本字段，不返回原始 `state_json`、`reset_event`、`reset_history`、`previous_state_json` 或自由文本 `reason`；
8. 授权扇出：每用户/角色/租户可见的 device/run 数量与上限、总映射规模、热点分布、历史 run 及跨 run 路由/分页上限；
9. 正式只读路径：使用现有 `connect_readonly()` 打开已存在数据库，不调用可写连接、不初始化 Schema、不执行 DDL/DML，也不在请求路径运行会写统计信息的 `ANALYZE`；
10. 查询计划与索引：对冻结后的真实 SQL、现行一 run 一库拓扑及代表性 D0/D1/D2 分别保存 `EXPLAIN QUERY PLAN`、冷/热态和统计信息证据，再决定沿用现有索引、在维护阶段生成统计、另立索引变更或引入聚合读模型；不在证据前预定答案；
11. 缓存 key 必含权限域、查询参数和具名版本/可见性边界，拒绝结果不得共享泄漏；
12. OS/SQLite 冷缓存、应用冷缓存和稳定热态的可重复准备/清理；
13. 缓存容量、失效、逐出与禁用后的基线。

C4 在 [#133 合入后复核](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/133#issuecomment-6762781638) 中使用单 SQLite、单 run、多 device、40 万条 `alarms` 的合成探针。**同一台机器、同一数据集、同一循环次数 N=50** 的三态对照为：未 `ANALYZE` 约 23.575 秒、已 `ANALYZE` 约 0.020 秒、已 `ANALYZE` 且补复合索引约 0.013 秒（同批对照，绝对值随机器负载浮动，仅用于说明查询计划对统计信息的敏感性，不作为生产基线）。C4 已更正其早先跨批次引用（48.03 秒为 200 次口径），不得再引用“约 3700 倍”跨批次倍差。该探针拓扑不同于现行一 run 一 device / 每 run 一库，也不得据此冻结新增索引。

任一错误结果、越权或跨权限泄漏均为零容忍停止条件。

### 5.1 #135 认证与授权契约（保留 #137 送审记录）

> 来源：[B1 建议 6763623979](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/135#issuecomment-6763623979) 与 [A 当时的待评审裁定 6764033066](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/135#issuecomment-6764033066)。本节保留 A/B1 当时提交 Review 的候选方案；#137 契约与 #149 查询实现现已合入。以下候选措辞不得替代正式 Review，也不得据此宣称生产认证或容量验收完成。
>
> 本节只定义契约，不生成、签发、提交或配置任何密码、bearer token、HMAC 密钥或其他凭据。所有示例都只使用不可运行的 `<PLACEHOLDER>`。

#### 5.1.1 信任边界与职责

认证（确认主体）与授权（主体可访问哪些 run/device）必须分开：

1. 查询入口只从 `Authorization: Bearer <BEARER_TOKEN>` 接收 bearer；不得从 URL query、请求正文或 cookie 接收，也不得接受客户端另传 `sub` 覆盖 token 主体。
2. 内部签发器只接受可信 fixture/bootstrap 已确认的主体并签发短期 token；第一版不公开 `login` 或 token 签发网络端点。fixture 注入“已认证主体”只证明测试边界，不等于已实现生产身份校验。
3. 查询服务先严格验签和校验主体/时效，再以 `sub` 查询静态 YAML 授权快照；run/device 权限、数据库路径和角色列表均不得写入 token。
4. YAML 只负责授权，不能证明身份，也不得保存密码、token 或签名密钥。
5. 生产签发者如何验证原始凭据、TLS 终止位置、运行时密钥供应和轮换尚未冻结；这些事项解决前，只能声明 D0 测试/开发认证，不得声明生产登录已完成。

bearer 仅凭持有即可使用，非 loopback 部署必须通过 TLS 或受信 TLS 终止；传输和存储均不得泄露凭据。参考 [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750.html)。

#### 5.1.2 短期 bearer 格式候选

仅允许以下占位结构；这是项目自定义 token，**不得称为 JWT**：

```text
g15q1.<BASE64URL_CANONICAL_CLAIMS>.<BASE64URL_SIGNATURE>
```

- 服务端固定 `HMAC-SHA-256`；token 不携带、也不能选择 `alg`。格式段固定为 `g15q1`，claims 中 `typ="g15-query-access"`、`ver="1"`；具体 `iss` 与 `aud` 值仍待 Review。
- claims 只允许 `typ`、`ver`、`iss`、`aud`、`sub`、`iat`、`exp`、`jti`。`iss` / `aud` / `sub` / `jti` 必须为非空、区分大小写、长度受限的 ASCII 标识；`jti` 由服务端 CSPRNG 生成，至少 128 bit 随机量，但只作审计关联，不提供防重放保证。
- 签发端用 `json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)` 产生唯一 ASCII JSON 字节，再使用**无 `=` padding** 的 RFC 4648 base64url；拒绝缺失、重复或未知字段、非对象 JSON、浮点/布尔时间、非规范 JSON/base64url、非法字符和超过候选 4,096 bytes 的 token。
- 签名输入为原始 `g15q1.<BASE64URL_CANONICAL_CLAIMS>` ASCII 字节；必须校验完整 32-byte HMAC-SHA-256 digest，不允许截断。签名比较使用 `hmac.compare_digest`，不得用普通 `==`。[Python `hmac` 文档](https://docs.python.org/3/library/hmac.html#hmac.compare_digest) 同样建议用 `compare_digest` 降低时序分析风险。
- `iat` / `exp` 使用服务端写入的 UTC POSIX 整秒；必须 `exp > iat`。候选默认 TTL 为 600 秒、硬上限 900 秒、服务器时钟容差 ±30 秒，均待 C4 复核，不是冻结门槛。
- 校验顺序为：严格检查唯一 header、三段数量、总长度和 ASCII/base64url 字符集 → 校验固定 `g15q1` → 解码 signature 并要求恰为 32 bytes → 对原始前两段计算 MAC 并 `compare_digest` → MAC 通过后再解码/严格解析 JSON、拒绝重复键，并重编码确认 canonical payload 段逐字节一致 → 最后校验 typ/ver/issuer/audience/主体和时间。验签前解析出的内容一律视为不可信输入。
- 当前签名 key 只能由待审的运行时秘密来源注入，必须至少 32 bytes 高熵；短 key 或缺 key 时服务拒绝启动。key 不得出现在仓库、YAML、静态 fixture 数据、日志、URL、CLI 参数或默认值中。未来测试 fixture 只能在进程内注入临时测试值，不能把该值提交为凭据；不得回退到硬编码值。本 PR 不创建该测试值。
- bearer 签名 key 与分页 cursor 的完整性 key 必须目的隔离；本节不生成或选择任何 key。

#### 5.1.3 授权 YAML 候选

授权文件只展示结构占位符：

```yaml
authz_schema_version: "1"
policy_version: "<OPAQUE_POLICY_VERSION>"
subjects:
  "<OPAQUE_SUBJECT_ID>":
    actor_ref: "<OPAQUE_ACTOR_REF>"
    permission_domain: "<PERMISSION_DOMAIN>"
    allow:
      - run_id: "<RUN_ID>"
        device_id: "<DEVICE_ID>"
```

加载与使用规则：

- 使用安全加载并严格校验：拒绝未知/重复键、YAML tag/merge/alias、空或非字符串主体、通配符、重复授权对及大小写模糊匹配；任何错误都拒绝整个快照并 fail closed，不做部分加载。
- `sub` 必须精确匹配非 PII 的稳定主体 ID；同一条目还必须提供唯一、非 PII 且不等于 raw `sub` 的 `actor_ref`，日志只使用该值。`policy_version` 与 `permission_domain` 也必须是非 PII 的不透明标识。姓名、邮箱和真实个人标识不得进入授权文件或日志；禁止用无盐 `hash(sub)` 伪装匿名化。
- D0 每主体最多授权 10 个不同 device；每条授权项是一对 `run_id + device_id`，每次请求只查一对。允许同一 device 存在多个历史 run，但第一版不做跨 run 查询、排序或聚合；超出 device 上限属于授权快照加载失败，不映射成单次请求的 401/404。
- 客户端不得提交数据库路径。服务将 `run_id` 映射到受控 run 根目录，解析后必须仍位于根目录内，并核对 Manifest 的 `run_id/device_id` 与请求一致；拒绝绝对路径、`..` 和符号链接逃逸。
- 授权快照第一版只在启动时加载；变更后需受控重启。每一页请求仍重新认证并按当前快照授权，token 不缓存权限结论。热加载与失败回滚另行评审。

#### 5.1.4 外部错误与资源隐藏

| 条件 | 外部结果 | 边界 |
|---|---|---|
| Authorization 缺失/重复、scheme 错、格式/签名/claim 错、过期或时钟越界 | 401 | 通用 `WWW-Authenticate: Bearer`；正文不区分具体失败原因 |
| token 有效但主体无授权、授权主体不存在、run/device 不存在或 Manifest 不匹配 | 404 | 状态与正文结构一致；先授权再访问数据库，不泄露资源是否存在 |
| 参数或 cursor 结构无效且不涉及权限域泄漏 | 400（候选） | 通用参数错误；具体错误 schema 待 C4 复核 |
| 有效且获授权 | 进入查询 | 第一版不引入 403 |

内部日志可记录有限的枚举原因码，但不得记录 Authorization、token、签名、raw `sub` 或资源详情；本契约不承诺 404 恒定耗时，只冻结外部状态与正文同构。

#### 5.1.5 查询、分页、版本与只读边界

- 第一版只支持单 run / 单 device / 每 run 一库；跨 run、共享目录、聚合读模型和全局快照不在 #135。
- 每一页都重新验 token、查 YAML 并用 `connect_readonly()` 打开已存在数据库；不得初始化 Schema、执行 DDL/DML/`ANALYZE`，缺库也不得创建文件。
- 窗口按 `start_seq DESC`；告警按 `start_seq DESC, rule_id DESC`。不得用仅 OFFSET 分页。
- 窗口与告警使用两个独立 cursor。首屏分别记录 `window_high_water_start_seq` 与 `alarm_high_water=(start_seq, rule_id)`；后续页只返回对应高水位以内的行。每个 cursor 必须绑定 `cursor_version`、`actor_ref`、`policy_version`、权限域、查询类型、run/device、排序、`page_size`、高水位和上一页最后一键；窗口最后一键为 `start_seq`，告警为 `(start_seq, rule_id)`。结构损坏返回候选 400，跨主体/权限域/scope 使用统一 404。
- descending keyset 谓词必须可复现：窗口后续页使用 `start_seq < last_start_seq` 且不超过窗口高水位；告警使用 `start_seq < last_start_seq OR (start_seq = last_start_seq AND rule_id < last_rule_id)`，且不超过告警高水位。稳定分页依赖当前 `feature_windows` / `alarms` 对已处理 run 的追加式写入；若未来允许回填更小排序键、更新或删除，必须使旧 cursor 失效或另行冻结快照机制。
- cursor 是不可信输入且必须具备完整性保护；编码、长度和目的隔离 key 的来源仍待 C4 复核。每页授权重查不可由 cursor 替代。
- 候选 `page_size` 默认 50、最大 100，待 C4 复核。
- 版本来源必须逐项具名：`manifest_schema_version` 来自目标数据库 `runs.schema_version` 的实际持久化值，不得硬编码；当前写端为 2.0，兼容读入可能为 1.0。`features_version` 来自每条 `features_json`；`state_schema_version` 来自 `state_json` 信封；`scoring_version` 与 `rule_id` 不是 Schema 版本。配置版本没有可追溯持久化来源时必须省略，不得硬编码 `1.0` 或从 Manifest 推断。
- 健康查询可返回契约允许的窗口/告警业务字段；状态部分只能从 `state_json.state` 做允许列表投影并附获准版本字段。不得返回原始 `state_json`、`reset_event`、`reset_history`、`previous_state_json`、自由文本 `reason` 或 `private_truth`。
- 新鲜度只依据当前只读事务可见的已提交序号/状态/持久化时间；服务观察时间不得冒充数据生成时间。

**#162双流选择候选（2026-11-13）**：精确外部shape及错误顺序见[interface §17](../interface.md#17-双流显式选择与发送前准备1622026-11-13-候选)。不传`streams`仍为query_contract_version`"1"`原shape/原行为；显式`both/windows/alarms`为`"2"`，两个分页对象各增严格bool`omitted`。未选流`items=[]`、cursor和对应流/顶层高水位null，仍保留sort/page_size；omitted不是无数据。状态和其提交高水位照实读取，未选windows时返回窗口版本集合为空。未选流带任何非None cursor均400；认证401/资源隐藏404仍先于可选参数校验。选中流维持原签名scope和高水位，旧v1 cursor可按相同scope继续v2选择；不重新采快照来替换旧cursor。遍历在一流耗尽后只请求另一个流，D0目标raw100窗/1告警、discard0，独立oracle先验证全部原始条目与请求期望，再验证跨页完整性。此为产品query候选，须真实非作者Review，不改变旧T17710/1/9事实，也不宣告Gate0通过。

#### 5.1.6 失效、撤销与重放边界

- 第一版是无状态 bearer：同一有效 token 在 `exp` 前可重放；`jti` 只供审计关联，不自动阻止重放。短 TTL、issuer/audience 绑定、TLS 与禁止记录凭据只能降低风险，不能宣称已消除。
- 第一版没有 refresh token、逐 token 撤销、denylist、会话或 replay cache。若这些成为要求，须另立契约与状态存储 Review。
- 第一版只支持单一当前签名 key；**替换运行时 key 后**受控重启会使全部旧 token 失效，仅重启而沿用同 key 不会失效。不能声称支持单 token 紧急撤销。
- 移除 YAML 主体并重启后，该主体即无授权；其未过期 token 仍可能通过认证，但所有资源请求统一 404。
- 生产签发/bootstrap、密钥来源与轮换、TLS 终止、紧急撤销及 cursor 完整性机制均是生产声明前的阻断未决项。

#### 5.1.7 脱敏追踪占位样例

服务端生成 `request_id`；第一版 `trace_id` 与其相同并贯穿 `server_received → authn_checked → authz_checked → db_opened → query_started → query_completed → response_completed`，拒绝路径使用 `rejected`。ID 由 UTC 前缀与安全随机片段组成，不接受客户端值作为权威 ID；`actor_ref` 只取自已校验授权快照的独立字段。样例不含真实数据：

```json
{
  "timestamp_utc": "<UTC_TIMESTAMP>",
  "request_id": "<REQUEST_ID>",
  "trace_id": "<TRACE_ID>",
  "actor_ref": "<OPAQUE_ACTOR_REF>",
  "permission_domain": "<PERMISSION_DOMAIN>",
  "stage": "<STAGE>",
  "query_shape": "single_run_single_device_health_v1",
  "manifest_schema_version": "<MANIFEST_SCHEMA_VERSION>",
  "features_version": "<FEATURES_VERSION>",
  "state_schema_version": "<STATE_SCHEMA_VERSION>",
  "result": "<STATUS>"
}
```

禁止记录 token、Authorization、签名/HMAC 摘要、密码、raw `sub`、真实姓名、`private_truth`、原始 run/device、cursor、reset 审计字段或自由文本 `reason`。401/404 对外只返回统一错误标识与 `request_id`。

#### 5.1.8 最低测试矩阵

| 组别 | 必须覆盖 | 预期 |
|---|---|---|
| header/格式 | 缺失、重复、错误 scheme、分段/base64/JSON 损坏、重复/未知 claim、超长 | 统一 401；日志无凭据 |
| 签名/claims | payload/签名篡改、错 key、错 typ/ver/iss/aud、空 sub、缺 claim | 统一 401；验签前不信任主体 |
| 时间 | 过期、未来 iat、exp≤iat、TTL 超上限、±30 秒边界 | 使用可注入时钟，不靠真实等待；按候选规则确定性通过/拒绝 |
| 授权/隐藏 | 允许、主体无映射、越权、资源不存在、Manifest 不匹配 | 允许路径正确；其余同结构 404；无 403 |
| YAML/路径 | 坏 YAML、重复/未知键、通配符、>10 device、绝对路径、`..`、符号链接逃逸 | 启动 fail closed；不部分加载、不创建数据库 |
| 生命周期 | 同 token 在 TTL 内重复使用、过期、整体换 key | 重放按已知风险留证；过期/旧 key 拒绝 |
| 分页 | 告警并列、翻页间追加、cursor 损坏/跨主体/跨 scope、翻页间撤权 | 无重复错序；新行不越过高水位；每页重查授权。证据限定：v1旧双流协议不能表达耗尽，省略 cursor 会重发首页；#136/T177 分列原始/消费/丢弃数量，客户端丢弃后的覆盖通过不代表服务端原始无重复。#162的显式选择v2候选另按请求上下文验证原始覆盖，不回写旧证据；本要求不降低 |
| 数据/只读 | 响应对独立 SQL/oracle；缺库；检查文件/Schema/业务行 | 结果一致；缺库不创建；查询路径零业务写入 |
| 版本/投影 | 各实际持久化版本来源、配置版本缺来源、reset 审计字段与真值扫描 | 返回实际版本并省略无来源 config 版本；敏感字段不可达 |
| trace | 全阶段 ID、401/404、日志扫描 | ID 可关联；无 token、PII、真值或审计原文 |

#### 5.1.9 Review 前仍未决定

1. `iss` / `aud` 的精确值、TTL/硬上限/时钟容差；
2. 生产身份凭据验证与 token 签发/bootstrap 流程；
3. 运行时密钥供应、TLS 终止、轮换、紧急撤销与是否需要 replay cache；
4. cursor 编码、长度、完整性算法及独立 key 来源，以及窗口/告警双高水位的确切编码；
5. 授权文件与受控 run 根目录的配置位置、热加载/重启策略，以及 `actor_ref` / `policy_version` 的创建与轮换治理；
6. 外部错误正文 schema、HTTP 路径/方法和 `page_size`；
7. D0 之后的授权扇出、历史 run、跨库路由与聚合读模型。

以上事项经 C4/B1 对当前精确 head 复核并由 A 记录后，才可称为冻结；此前实现只能针对获批的 D0 子集，不得宣称生产认证、即时撤销、防重放或十万级能力。

## 6. 架构选项

### A. Application query + 薄只读 transport（首选低负载验证候选）

新增不读真值的 application query，使用正式只读 repository 和 SQLite；传输层只做认证、授权上下文、参数/响应映射、超时和背压。先证明 D0/低负载正确，再依据瓶颈决定是否采用 HTTP、进程/线程/异步模型、连接池或读模型。

优点：复用真实持久化数据、最小增量、能建立正确性基线。限制：每 run 一库和 SQLite 并不自然证明高规模；跨 run 查询和共享目录仍未解决。

### B. 仅 CLI 批查询

可作为调试/回归入口，但没有多用户传输、认证、连接或在途请求语义，不能作为十万用户容量方案。

### C. 直接引入微服务、共享读模型或 Redis

可能成为容量演进方向，但在低负载真实路径、查询计划和瓶颈证据前会扩大依赖、部署和一致性风险。任何新依赖须单独 Issue、ADR 与双 Linux 安装审查。

### D. 空 endpoint/固定响应压力测试

拒绝。它只测框架/网络，不能证明授权、查询、SQLite、正确性或日志链。

本 ADR 只推荐先用 A 建立真实低负载路径；传输框架、共享读模型、缓存和最终容量架构仍待 Review，不在本提案中冻结。

## 7. 并发、资源与可溯源证据

须冻结同步发起窗口、观测/统计区间、采样时钟与误差、开放式到达率或关闭式 VU、ramp/steady/burst/soak、worker/线程/进程/异步模型、连接生命周期、排队/背压和超时/取消。连接数不得默认为用户数。

每轮同时保存：

- client：不同用户数、barrier 释放、started/完整发送/waiting/completed、连接、客户端在途峰值、完成/失败/超时、原始延迟直方图、CPU/RSS/网络/FD；
- server：received/queued/executing/completed、服务端在途峰值及对应不同用户数、拒绝、队列、worker、CPU/RSS/网络/FD、SQLite 查询时延与 busy/locked/readonly、缓存命中/逐出、磁盘；
- trace：UTC、request/trace ID、匿名化 actor/权限域、各阶段时间戳、query shape、数据版本、workload profile、cache state、状态/错误码、DB/总时延和实例；
- provenance：完整源码 SHA、数据集摘要、配置、机器/环境、命令、退出码与全部失败样本。

发送 barrier 只允许在客户端开始发送前同步。不得通过服务端或客户端 `sleep`、故意延迟读取/写出响应、代理限速、人工阻塞 handler、压住响应或人为扩大队列制造在途峰值。快速完成导致服务端峰值低于目标时，应如实记录，不能修改业务路径来装饰数字。

禁止记录 token、密码、真实个人身份、敏感第三方信息或 `private_truth`。

## 8. 逐级验证与升级门禁

1. **单元**：输入/输出、认证授权、分页/limit、无数据、错误映射、cache key/失效、日志脱敏、timeout/cancel、指标守恒。
2. **集成**：真实进程、真实认证/授权、正式 query/repository、`connect_readonly()` 与真实 SQLite；逐项对照独立 SQL/oracle，不 mock 本次目标。
3. **冒烟/回归**：1 用户/10 请求，再到 10/50 in-flight、每档 1 分钟；响应和 client/server/request-log 计数守恒，同时保证既有生产/消费/报告及 #56/#9 不被改变。
4. **兼容**：Windows 开发回归；Ubuntu 24.04 与 RHEL 9 在同一 SHA 分别 fresh clone 做低负载启停、认证、查询和资源留证。十万级不进入共享 CI。
5. **性能**：主场景 F 候选先按相同阶段定义验证 100→500→1,000 个不同用户的实际在途请求；先预热，再按待审统计区间留证；冷/热、均匀设备和 80/20 热点分开。
6. **压力**：F 候选按 1k→5k→10k→25k→50k→100k 个不同用户逐级同步发起并实测客户端/服务端在途；目标档统计区间待 Review。H 的 VU/think time/soak 结果另报，不得混入或替代 F。

2026-11-12的Gate0补充候选采用test_plan §11.8的持续服务端、多真实波次观察：累计至少60秒质量通过的实际请求发送→完整读取并解码区间，墙钟与空档另列，所有波次峰值分布及失败保留；没有handler延迟、压响应或sleep。单波次10/50证据只证明该次瞬时在途，不替代本条一分钟观察，不外推持续恒定在途。工具CLI最低60秒，预算或任何质量失败停止；本观测统计判据仍待B1/C4真实复核后由A记录阶段结论。

2026-11-13的#162准备与阶段候选在[interface §17.3](../interface.md#173-d0量测准备与a候选阶段裁定)、test_plan §11.9登记。A对旧Win50 wave00192复核的last_accept/first_completion分别距first_received 28.3545/22.7892ms，故新tools adapter只观测真实handler reader安装就绪，经本wave精确四元组控制确认后才释放客户端发送前barrier；就绪不计received或已认证在途。headers在barrier前只内存拼装，计时从其后endheaders真实发送前起至完整读解码；建连/accept/reader/确认/barrier时间单列。服务端不等待、无barrier/延迟/响应hold/队列扩容，准备不能保证峰值N。A候选以received→response_completed验收D0 server在途，executing独立；absent/null的queued有条件N/A并保留backlog未测，N/A不等于门禁通过。AND、累计有效60秒和全部67旧未达波不变，新证据仍需固定SHA及B1/C4复核。

2026-11-13的后续客户端调度候选改用纯selectors单loop，精确规则见[interface §17.4](../interface.md#174-selector单loop客户端与有界证据写入162后续候选)与test_plan §11.10。exact ACK仍为前置，selector_backend及共同launch gate元数据单列；launch_mode/launch_released不复用线程barrier语义，gate不能冒充N次请求开始。每socket首次真实send调用尝试之前绑定start，包括当次EAGAIN零进度；从未send的准备失败只记独立client_attempt，不补accounting或latency。每步write后立即处理read-ready，完整body与JSON即complete，不等其余发送、EOF或journal。普通EAGAIN不加字节、不等同真I/O错；真实错误及既有5秒超时留证，数值不变，服务端业务/等待/队列不改。

主并发先用内存即时计数，N有界pairs队列交单证据写者；前置仍沿原持久化路径。drain/flush归gap及资源，不加入请求latency或有效时长；写失败以独立emergency保已知记录并失败，不重写原raw。A早响应而B短写/EAGAIN时，必须A先read/decode/complete再B发送完成，不能靠压读或延后complete造峰。新实现/workload另绑新SHA且不保证N，旧dba1个未达及T17767个未达不改判。AND/10/50/累计60秒、阶段、queued条件N/A、cache和socket层次边界保持，新成绩仍待固定源码回归、三平台各档一次实测与真实非作者Review。

网络资源候选以实际TCP连接的完整HTTP socket应用流字节及错误/生命周期留证，按两端反向四元组逐连接核对。它包含HTTP头/正文，排除TCP/IP开销、重传与物理链路余量；不能用总量巧合一致、正文大小或进程I/O代替逐连接证据，也不能据此宣称跨主机网络容量。精确层次与本阶段充分性须在真实资源/契约Review明确。

前一档的业务正确性、日志计数、负载端余量、服务端资源和缓存状态不可信时，禁止升档。

立即停止并保留证据的候选条件：

- 任一错误/越权/敏感信息泄漏或跨权限缓存泄漏；
- client/server/request-log 计数不守恒；
- OOM、崩溃、数据损坏、连接或事务泄漏；
- 5xx+timeout 连续 30 秒 >1%，或 p99 达到客户端 timeout；
- 服务端 CPU 连续 60 秒 >95%、内存 >85% 或持续 swap、FD >上限 80%；
- SQLite busy/locked/readonly 持续增加；
- 压测端 CPU/网络/FD 饱和或达不到计划 offered load；
- 时钟、原始日志、缓存冷热或统计口径不可证明。

停止后不得删失败、只报最好一轮或在同一结果上事后改门槛。

## 9. 责任、Review 与实施顺序

- A 与 B1 共同负责本增量需求、ADR、后续新增服务/权限/query 实现、测试计划、正确性 oracle、性能/压力报告和失败留存；
- 新增路径由 B1 承接；实现 Issue 必须明确文件白名单并同步 CODEOWNERS；
- 现有 storage、analysis、CLI、scripts 及 #56/#9 的属地保持不变；若方案必须修改这些文件，单独列影响并取得对应 Review；
- C4 负责首个需求/ADR PR 的必要非作者与公共契约 Review；
- A 做最终门禁，不自批；B1 的自测不能替代非作者 Review。

顺序：Issue #133 → 本需求/ADR 草案 → C4 与其他必要非作者 Review → A 门禁 → A/B1 拆新增实现 Issue → 单元/真实集成 → 低负载量测可信 → 容量/资源评审 → 另行授权高负载。CI 绿色、ADR 合入或关闭 #133 均不等于能力实现/通过。

## 10. 后果与未决项

正面后果：

- 把课堂侧重点转成可审、可量测且不会与百万点/#56混淆的增量；
- 先以真实数据和权限正确性建立基线，再决定扩容技术；
- 明确失败留存、两端饱和诊断和双 Linux 证据。

成本与风险：

- 送审时需要新增身份/权限、服务、日志、负载和部署能力；其中首版认证/授权、HTTP 查询与脱敏追踪已由 #149 交付，容量、生产认证与部署取证仍须后续完成；
- 每 run 一库的布局可能阻碍跨 run/多设备查询；
- 严格 100k in-flight 可能需要分布式压测和服务架构，不能由当前单 SQLite 推断。

历史送审待决清单（保留原问题）：

1. 主场景 F 的同步发起窗口、客户端/服务端在途状态边界、采样时钟、统计区间与判定阶段；H 仅为补充场景；
2. 第一版仅查询单 run/device，还是需要共享目录/跨 run；若跨库，如何表达各库 `as_of` 与非全局原子快照；
3. 身份/授权模型、资源隐藏语义、授权扇出上限、热点分布与跨 run 路由；
4. application query 与 transport 边界、依赖、进程/连接模型；
5. 确定性排序、稳定游标、提交态可见性与分页期间新提交数据的语义；
6. Manifest/config/feature/state 四类版本字段的名称、来源与缺失行为，以及健康响应的审计字段允许列表；
7. 正式 `connect_readonly()` 路径、代表性真实 SQL、D0/D1/D2 查询计划与逐表索引/统计维护决策；
8. D0/D1/D2、告警密度、响应大小、缓存策略与 SLO；
9. 低负载通过后允许的资源/拓扑及何时授权十万级；
10. （已确认，不再待定）`syc` 仅为 `src` 的笔误；`src` 与 `test` 的对应关系按 B1 复核记录。
11. 概要/详细设计、TOGAF 映射和 Windows/Linux 部署材料如何从 G15 自有事实派生。

清单中的首版单 run/device、认证/授权、只读查询、分页/版本/投影和 HTTP 边界已有 #137/#149 的合入内容与正式 Review，不再视为全部待实现；同步/统计/SLO/资源、代表性查询计划与容量执行仍按 #136 审查和取证。不得把候选数字、已合入文档或低负载实现写成生产认证、十万级冻结标准或验收成绩。
