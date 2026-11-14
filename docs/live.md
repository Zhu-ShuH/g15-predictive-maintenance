# 十台仿真设备持续采集与分析

2026-11-14 新增[回放与运行对比工作台](workbench.md)。持续入口默认启用 `/workbench`，从明确的输出根登记已结束会话，并将本机告警处置记录保存在 `<output-root>/workbench`；可通过 `--operations-root` 指定独立目录。实验故障注入仍通过 `--laboratory-root` 单独启用。

此入口按墙钟节拍生成新的仿真样本，接现有窗口特征、评分、告警、SQLite和真实认证查询。它不接真实硬件，不预生成整段数据后回放，不修改离线Manifest 2.0的complete语义，不声明独立保留集或Gate0通过。

## 启动与停止

在业务仓库外准备运行目录；不可用Obsidian规划库或output作为测试、缓存或运行目录。安装现有锁定依赖后运行：

~~~powershell
python -m g15.live --output-root D:\work\repos\g15-live-runtime --health-rules configs/health_rules.yaml --devices 10 --sample-rate-hz 256 --window-points 256 --interval-seconds 1 --duration-seconds 3600 --max-disk-mib 512 --console-port 18468 --query-port 18469
~~~

控制台和查询仅绑定127.0.0.1。服务端在内存签发短期Bearer，浏览器和运行目录没有令牌或密钥文件；单一身份最多授权10台设备的现有约束不变。

output-root/active.json记录session、端口、PID、源码SHA/dirty和stop_file。向stop_file写内容或对前台进程按Ctrl+C可正常停止：先停新采样，再处理已采任务；超时或失败不能写成正常停止。达到时长/磁盘保护后为limit_reached，网页继续显示已存结果；--exit-after-stop使HTTP也退出，供有界实测使用。

默认每台256Hz、每1秒256样本、一个256点窗口；合计2560样本/秒与10窗口/秒只是配置负载，不是测得的最大容量。一个样本含振动和温度两个标量。周期扰动是公开演示输入，不为视觉效果改阈值，不读取private_truth。

### 仿真采集启停

本机控制台支持暂停/恢复单设备或全部设备的仿真采集。暂停确认后停止产生新样本，已落盘且接受的批次仍会分析并写入SQLite，因而暂停后处理计数可能继续增加直到追平。恢复沿同一run的下一样本序号继续，暂停期间不补生成数据，不重置健康评分、告警状态或历史。

暂停不结束会话，整体运行状态仍为running；以每设备 `capture_paused` 判断是否采集。时长和磁盘保护继续生效，达到保护或会话已停止后不能通过恢复绕过保护。控制只作用于当前本机仿真，不发硬件控制命令。控制请求不调用通知测试或修改通知设置；独立运行的已有自动提醒策略保持原语义。

控制契约为 `POST /api/live/control`，传当前 `session_id`、`action=pause|resume`；单设备另传精确 `run_id/device_id`，全体省略这两个字段。必须使用同源 Origin、JSON正文及 `X-G15-Console-Action: live-control`。会话改变或终态返回409，用户应刷新状态；GET `/api/live` 的 `controls_available` 表明是否配置控制能力。界面上的吞吐速率应从真实采集/提交计数与观测时间差计算，首个观测、重连、会话变化与负增量不推断速率。

## 协议与状态

会话目录唯一，每设备独立run与SQLite；同一output-root的写者由操作系统文件锁互斥。采样按绝对序号产生，原始批次持久化后进入有界队列，分析按序提交特征、评分、告警、状态和处理标记；告警状态跨批次延续。积压、失败和未处理样本明确记录，不能静默丢弃后冒充追平。

live-session.json是独立持续描述，不是manifest.json；两者同时存在时拒绝。描述schema_version=1，source_kind=wall_clock_simulator；数据库runs.status固定streaming只表示数据种类，真正起停和存活须看实时状态。旧离线消费者仍只接受complete Manifest 2.0。

HealthQueryApplication仅显式allow_live_runs=True才接纳live。身份、路径、描述和数据库元数据须一致。原认证、权限隐藏和独立分页高水位保留；新提交不混进已有游标，每请求仍只查询一个run/device，不承诺跨设备原子快照。

/api/live仅内嵌运行时提供，静态控制台返回404；公开起停、心跳、采样率及每设备实收/提交样本、窗口、告警、待分析批次、最近提交。自由错误在网关替换为固定提示。网页一轮完成后再间隔2秒轮询，概览并发最多3，隐藏暂停；不覆盖历史、全程、报告或手动窗口检查。缺失、断连、停止或过期不能沿用绿色实时状态。

持续报告是已提交SQLite的运行摘要，verifiable=false，passed/detection_rate/false_alarm_count均null；不是既有真值匹配报告。状态/报告原子写入，最终失败保留，不以旧文件冒充新结果。

## 首轮实测方案（执行前登记）

固定clean SHA、单Windows主机、10台公开仿真设备、256Hz、256点窗口、1秒批次、队列20、约75秒运行、512MiB磁盘上限；首个有效窗口之后观察至少60秒，另记录启动、墙钟和停止。10设备不能换算成#136的10/50查询用户或更高容量档位。

保留每设备实收/提交样本、窗口/告警、原始批次与SQLite连续序号和摘要对账、积压、失败；轮询真实API证明持续推进。保存逐批生成开始到SQLite commit返回的原始纳秒延迟与分位数、HTTP延迟、同进程CPU/RSS/handle或FD和磁盘观察。该延迟不是硬件采样延迟，资源峰值只是已观察值。

本轮要求10台都持续推进至少60秒、无静默丢样/重复窗口/状态回退/SQLite损坏/HTTP错误；正常停止后captured=processed且queue_depth=0。失败原样留存，不重跑择优、放宽阈值或扩队列造通过。未设工业SLO，不声明最大容量。实测、CI、双Linux兼容与内部界面复核分别记录，不能代替真实非作者Review。

## 波形和独立实验入口

持续服务可附加 `--laboratory-root <独立实验目录>` 启用 `/laboratory` 的场景创建；不指定时该页仍可读取登记设备的已提交原始样本。实验参数和运行目录独立于持续会话，不修改当前设备采集配置；退出服务会关闭实验 worker，实验记录跨重启保留。波形在 SQLite 已提交范围内核对原始批次 SHA，参数与限额见 [波形与实验台](laboratory.md)。
