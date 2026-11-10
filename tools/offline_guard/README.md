# Python 运行期离线守卫（仅测试）

把本目录放到 `PYTHONPATH`，并设置 `G15_DENY_NETWORK=1`，Python 启动时会
通过 `sitecustomize.py` 注册审计钩子，拒绝标准库 `socket` 的 DNS、建连、
绑定和发送等事件。未设置该变量时文件不改变任何行为，生产入口不会自动启用。

该守卫用于自动化测试“主链没有调用 Python 网络 API”。它不是操作系统防火墙，
也不能排除绕过 Python 审计 API 的原生代码；最终现场证据仍须在最终提交 SHA 上
关闭网卡，或使用隔离网络命名空间后重新执行 `scripts/acceptance.sh`。
