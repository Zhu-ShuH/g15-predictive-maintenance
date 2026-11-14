# 本机只读网页控制台（#158 候选）

接口与范围见[interface §18](interface.md#18-本机只读网页控制台158-候选)；§17 保留给双流查询与发送前准备契约。

2026-11-14 用户授权的扩展另见[故障注入、回放与运行对比](workbench.md)：`/workbench` 可读取已提交分析数据并将告警确认及备注写入独立处置库；本页原只读协议及分析数据写入边界保持不变。

控制台独立代理已运行的真实查询 HTTP API，并展示已有公共报告。它不生成数据、不评分、不消费、不读 SQLite 或 `private_truth`，也不替代正式验收。此页描述本次候选行为；真实评审、CI、部署验收状态由对应 PR/Issue 记录。

## 启动

在项目已安装的 Python 3.12 `pp03` 环境中运行：

```sh
python -m g15.console \
  --catalog runtime/runs.json \
  --runs-root runtime/runs \
  --query-base-url http://127.0.0.1:18465 \
  --token-file runtime/credentials/token.txt \
  --host 127.0.0.1 \
  --port 18467
```

示例中的 `runtime` 是部署者选择的外部运行目录，不要求放入仓库。原查询服务必须先运行；本入口不会启动或修改它。控制台端口和 API 端口分开，浏览器打开控制台打印的本机 URL。静态 HTML/CSS/JS 通过包资源读取，随安装包分发；不要求调用者工作目录内存在 `static/`。

`--query-base-url` 仅接受 `http` 数字 loopback 地址，可带端口，不能含用户名、密码、路径、query 或 fragment；不经环境 HTTP proxy，不跟随重定向。`--host` 也必须是数字 loopback 地址。CLI 配置不合法返回参数错误；它不会自动安装软件、改防火墙或暴露外网服务。

## 注册资源

`--catalog` 是 UTF-8 JSON 数组，例如：

```json
[
  {
    "run_id": "example-run-1",
    "device_id": "DEV-001",
    "scenario": "healthy",
    "total_points": 200000
  }
]
```

每个 run 仅注册一个设备，run/device 标识只接受不超过128字符的 ASCII 字母、数字、下划线、连字符与内部点号。精确注册的 pair 才能查询或下载。公开字段为 `run_id`、`device_id`，以及 catalog 实际声明的 `scenario`、`scenario_id`、`total_points`、`sample_rate_hz`、`start_time_utc`；未知字段不会传给浏览器，不根据目录名补造元数据。空数组是合法空态。

报告固定在 `<runs-root>/<run_id>/reports/summary.json` 和 `summary.md`。控制台不会创建、重算或改写它们。Markdown 使用现有报告生成器的 run/device 表格行作为身份核验；JSON 的 `run_id/device_id` 必须精确匹配所选资源。

## HTTP API

| 路径 | 参数和响应 |
|---|---|
| `GET /api/catalog` | 无参数；`{"devices":[...],"readonly":true}`，不包含凭据或文件路径。 |
| `GET /api/health` | 必须 `run_id/device_id`；`page_size` 默认100；可带非空 `windows_cursor/alarms_cursor`。对固定上游资源发真实认证 GET，合法健康查询及公开错误 JSON 保留原始字节，不改字段。 |
| `GET /api/report` | 必须 `run_id/device_id`；先对同一资源实际请求上游 `page_size=1`，仅200且身份匹配后读取固定 JSON 报告；报告原字节返回。 |
| `GET /api/report/download` | 必须 `run_id/device_id`；`format=json` 或 `format=markdown`，默认json；同样先授权探测，固定 `summary.json/md` 附件下载，不接受任意文件名。 |
| `GET /` | 固定首页；可带 UI 深链接 `device/run/view`，view仅 `health/alarms/maintenance/reports`，不反射这些值。选择是否合法由前端 catalog 状态处理。 |
| `GET /screen` | 深色大屏驾驶舱；可带 `device/run` 选择参数，其他参数拒绝。 |
| `GET /operations` | 浅色运营看板；可带 `device/run` 选择参数，其他参数拒绝。 |
| `GET /diagnosis` | 单设备诊断工作台；可带 `device/run` 选择参数，其他参数拒绝。 |
| `GET /simulation` | 仿真采集控制与数据流看板；可带 `device/run` 选择参数，其他参数拒绝。 |
| `GET /assets/app.css`、`/assets/app.js`、`/assets/state.js` | 控制台固定包资源，不接受 query 或其他资产路径。 |
| `GET /assets/fleet.css`、`/assets/fleet.js` | 大屏与运营看板固定包资源，不接受 query。 |
| `GET /assets/diagnosis.css`、`/assets/diagnosis.js` | 诊断工作台固定包资源，不接受 query。 |

以上读取路径也支持 HEAD，响应体为空但仍执行相同授权检查。除独立文档定义的通知POST与下述仿真控制POST外，其他方法返回405；读取响应关闭连接，不接受请求体、Transfer-Encoding、重复 Content-Length 或 Expect。重复/未知 API query 参数返回400，未注册 pair 返回404。

内嵌持续会话另外提供 `/api/live` 与 `POST /api/live/control`。控制JSON为当前 `session_id`、`action=pause|resume`；可选成对 `run_id/device_id` 选择单设备，省略则控制全体。正文最多4096字节，要求同源Origin、JSON Content-Type与 `X-G15-Console-Action: live-control`，未知/重复字段拒绝。200返回公开持续状态；409表示会话改变或运行已终止，503表示未配置控制或暂不可用。暂停仅停止新的仿真采集，已接受分析继续排空；恢复不清历史、不补样本、不重置序号。当前会话启用控制时公开 `controls_available=true`，设备公开 `capture_paused`；未提供的控制能力不应显示为可操作。详见[持续采集说明](live.md)与接口契约§19。

合法上游401、404、503等保留状态与既有公开错误信封。连接/超时或暂不可读的凭据返回脱敏503；重定向、超限、错资源、Bearer反射或非公开错误信封返回502。错误不回显异常文本、磁盘路径、traceback或凭据。有效上游响应不因这些异常检查被重新编码。

## 本机信任边界与限制

Bearer 由服务端每次上游请求前读取 `--token-file`，因此后台原子更新凭据后新请求立即使用新值。浏览器不输入、不接收、不保存 Bearer；不会将其放在 URL、catalog、日志或下载中。包含当前 Bearer 的上游内容即使状态为200，也会拒绝。

服务仅面向本机可信使用者，不提供公网登录、用户管理或多租户隔离。能运行本机进程的账号仍处于本机信任边界内。catalog、包文件、凭据与运行产物须由部署者控制写权限。资源白名单和成功授权探测并不证明产物的科学结论正确；错资源报告、符号链接/reparse目录与文件、逃逸路径和读取期间更换的文件身份均被拒绝。

Host 必须匹配本机监听地址或localhost及实际端口；Origin存在时须与该Host同源；Fetch-Site只允许same-origin/none。无通配 CORS。CSP只允许同源脚本、样式与连接，无inline/eval；另有nosniff、no-store、same-origin资源策略和no-referrer。

默认最多16个请求worker；请求头总读取上限32KiB、target上限8KiB、头部绝对期限3秒；上游连接至完整响应也有3秒总期限。catalog上限128KiB/1000行、凭据上限8KiB、单次上游/报告/资产读取上限8MiB。超大或慢头可能在HTTP解析阶段直接关闭；过载503，不扩张无界线程。上述是本机控制台保护，不是容量验收成绩。

报告的 `verifiable=false`、`passed=null` 必须保持不可判定；不能根据healthy/degradation目录名推断验收通过。事件捕获判定 `passed=true` 也不等于无误报、当前设备正常或真实工业诊断完成。当前告警状态、历史告警、报告判定应分别展示。

## 大屏驾驶舱与运营看板

控制台导航可进入 `/screen` 深色大屏和 `/operations` 浅色运营看板，两页也可直接打开并互相切换。两页复用同一目录、真实健康预览、活动状态、窗口和可选持续状态；不新增查询 API、不读取额外文件，也不向浏览器暴露 Bearer。

大屏提供实际设备数的状态分布环、异常优先设备矩阵、所选设备真实健康趋势、来源明确的维护建议和逐设备采集状态。点击矩阵设备会重新查询该设备并更新深链接；趋势和建议可进入原控制台对应视图。运营看板提供实际状态指标卡、所选设备趋势及按设备/运行标识搜索的设备表，每行可进入健康详情或维护建议。

状态统计使用现有 `deviceStatus` 语义：红色、黄色、无活动告警、未知/过期和读取失败分别计数，不能由健康分自行推导告警级别。无持续会话时采集计数显示 `—`；窗口曲线只画当前已加载范围。页面没有租户切换、方案卡/工单数量、登录、会诊或设备控制，因为当前交付没有这些真实数据源与写入契约。

前端汇总和链接测试使用 `node --test tests/frontend/*.test.mjs`；路由、HEAD、资源白名单和参数拒绝由 `tests/integration/test_console_service.py` 覆盖。真实浏览器验证仍须绑定最终候选提交。

## 维护建议

`maintenance` 视图在浏览器端把当前已读取的真实告警映射为只读检查建议，不增加查询字段或业务写入。每个规则优先选用与当前活动级别一致的来源告警；无活动告警时选用最新历史记录。卡片保留规则、窗口位置、原始原因、已知扣分特征、建议检查项和来源告警跳转。

红色活动告警只提示优先进行现场安全核查，是否停机由人员按现场规程决定；黄色活动告警提示近期点检与复测；历史记录提示复核处置和复测。特征映射只覆盖现有八项振动特征，无法识别原因中的特征时使用通用复核建议，不推断内圈、外圈等具体故障。

加载中、读取失败、活动状态未知和空记录分别展示。告警仍有后续分页或活动规则缺少对应级别的来源记录时，页面明确提示覆盖不完整，不补造建议。所有建议均需人工确认；界面不创建工单、不保存确认状态、不停机或控制设备。

## 诊断工作台

`/diagnosis` 使用与其他页面相同的真实目录和查询状态。设备选择、异常设备快捷选择及“读取最新已存结果”只触发既有只读请求，不启动在线诊断或新仿真。结果区显示当前已存健康分、活动状态、评分/规则版本、窗口范围和采样率。

五维分解使用最新已存窗口在评分时写入的 `risk_dimensions`：电平偏移（mean）、振动强度（rms）、波动程度（std）、冲击特征（ptp/kurtosis_excess/crest_factor 取最大风险）、频谱变化（dominant_freq_hz/zero_crossing_rate 取最大风险）。这些是现有特征的未加权风险分组，不参与或重算健康总分。

新写入的特征信封版本为 2；版本 1 没有保存五维，页面显示不可重建。组内特征未配置时对应维度为 `null`，不按 0 绘制；五个维度均有有效 `[0,1]` 风险时才绘制五维雷达，否则使用逐项风险条和未知文案。来源告警仍用于证据链和维护建议，不与最新窗口五维混为同一时刻。

证据链显示当前已加载的最近五条告警，保留级别、规则、采样窗口、记录时间和原始原因；来源告警高亮，分页未尽明确提示。可跳回运行报告或维护建议。当前没有五维温度/电流模型、SLA、在线会诊或设备执行契约，因此页面不提供这些入口。

诊断解析、历史/活动边界及空态由 `tests/frontend/diagnosis.test.mjs` 覆盖；真实浏览器验证须检查设备切换、来源标记、证据链接与窄屏。

## 验证

必要集成测试使用库外临时目录中的真实认证查询 HTTP 服务、公开 D0 SQLite fixture 和合成报告，不接触现有运行服务或真实凭据：

```sh
python -m pytest -q tests/integration/test_console_service.py tests/integration/test_query_service.py --basetemp <库外新临时目录>
python -m black --check src/g15/console tests/integration/test_console_service.py
python -m pylint src/g15/console --fail-under=8.5
```

覆盖原样代理、每请求凭据更新、401/404/503、报告先授权/身份错配、目录与文件逃逸、文件更换、Host/Origin/Fetch-Site、无Bearer反射、只读方法、资源与静态白名单、深链接、过载、非整数worker拒绝、上游大小/总时限、重定向拒绝和空态。Windows无创建symlink权限时相应用例明确skip，Linux应实际执行，不能把skip写为通过。完整项目回归、真实浏览器与双Linux安装验证另按最终SHA执行，不以这些定向测试代替。

## 波形观察与场景实验

`/laboratory` 在同一工业控制台中展示已提交的振动、温度和频谱，并管理独立参数实验。实时读取仅限已登记的运行与设备，实验由显式启用的独立服务拥有。配置、接口、数值含义和限额见 [波形与实验台](laboratory.md)。
