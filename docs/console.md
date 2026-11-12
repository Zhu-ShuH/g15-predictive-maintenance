# 本机只读网页控制台（#158 候选）

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
| `GET /` | 固定首页；可带 UI 深链接 `device/run/view`，view仅 `health/alarms/reports`，不反射这些值。选择是否合法由前端 catalog 状态处理。 |
| `GET /assets/app.css`、`/assets/app.js`、`/assets/state.js` | 固定包资源，不接受 query 或其他资产路径。 |

以上读取路径也支持 HEAD，响应体为空但仍执行相同授权检查。其他方法返回405；所有响应关闭连接，不接受请求体、Transfer-Encoding、重复 Content-Length 或 Expect。重复/未知 API query 参数返回400，未注册 pair 返回404。

合法上游401、404、503等保留状态与既有公开错误信封。连接/超时或暂不可读的凭据返回脱敏503；重定向、超限、错资源、Bearer反射或非公开错误信封返回502。错误不回显异常文本、磁盘路径、traceback或凭据。有效上游响应不因这些异常检查被重新编码。

## 本机信任边界与限制

Bearer 由服务端每次上游请求前读取 `--token-file`，因此后台原子更新凭据后新请求立即使用新值。浏览器不输入、不接收、不保存 Bearer；不会将其放在 URL、catalog、日志或下载中。包含当前 Bearer 的上游内容即使状态为200，也会拒绝。

服务仅面向本机可信使用者，不提供公网登录、用户管理或多租户隔离。能运行本机进程的账号仍处于本机信任边界内。catalog、包文件、凭据与运行产物须由部署者控制写权限。资源白名单和成功授权探测并不证明产物的科学结论正确；错资源报告、符号链接/reparse目录与文件、逃逸路径和读取期间更换的文件身份均被拒绝。

Host 必须匹配本机监听地址或localhost及实际端口；Origin存在时须与该Host同源；Fetch-Site只允许same-origin/none。无通配 CORS。CSP只允许同源脚本、样式与连接，无inline/eval；另有nosniff、no-store、same-origin资源策略和no-referrer。

默认最多16个请求worker；请求头总读取上限32KiB、target上限8KiB、头部绝对期限3秒；上游连接至完整响应也有3秒总期限。catalog上限128KiB/1000行、凭据上限8KiB、单次上游/报告/资产读取上限8MiB。超大或慢头可能在HTTP解析阶段直接关闭；过载503，不扩张无界线程。上述是本机控制台保护，不是容量验收成绩。

报告的 `verifiable=false`、`passed=null` 必须保持不可判定；不能根据healthy/degradation目录名推断验收通过。事件捕获判定 `passed=true` 也不等于无误报、当前设备正常或真实工业诊断完成。当前告警状态、历史告警、报告判定应分别展示。

## 验证

必要集成测试使用库外临时目录中的真实认证查询 HTTP 服务、公开 D0 SQLite fixture 和合成报告，不接触现有运行服务或真实凭据：

```sh
python -m pytest -q tests/integration/test_console_service.py tests/integration/test_query_service.py --basetemp <库外新临时目录>
python -m black --check src/g15/console tests/integration/test_console_service.py
python -m pylint src/g15/console --fail-under=8.5
```

覆盖原样代理、每请求凭据更新、401/404/503、报告先授权/身份错配、目录与文件逃逸、文件更换、Host/Origin/Fetch-Site、无Bearer反射、只读方法、资源与静态白名单、深链接、过载、非整数worker拒绝、上游大小/总时限、重定向拒绝和空态。Windows无创建symlink权限时相应用例明确skip，Linux应实际执行，不能把skip写为通过。完整项目回归、真实浏览器与双Linux安装验证另按最终SHA执行，不以这些定向测试代替。
