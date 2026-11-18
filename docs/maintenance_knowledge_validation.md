# 维护知识库增补验证记录

日期：2026-11-17。基线：`936c127`。功能分支：`feat/maintenance-knowledge`。
本记录仅对应答辩后知识库增补，未合入，不改写冻结版原始测试报告。

环境：云端 Linux、Python 3.12.14、独立临时 venv；按仓库 requirements-dev.txt
安装锁定依赖。不是正式 Conda pp03，也不是 Ubuntu/RHEL 两系统复验。

## 最终文件检查

| 命令 | 结果 |
| --- | --- |
| `PYTHONPATH=src python -m pytest -q` | 2092 passed，15 skipped，174.46 秒，退出码 0 |
| `PYTHONPATH=src python -m pytest -q tests/unit/test_maintenance_knowledge.py tests/unit/test_notifications.py tests/integration/test_console_operations.py` | 102 passed，3.85 秒，退出码 0 |
| `node --test tests/frontend/*.test.mjs` | 135 passed，0 skipped，0 failed，退出码 0 |
| `python -m black --check src tests` | 128 文件通过，退出码 0 |
| `python -m pylint src/g15 --fail-under=8.5` | 9.67/10，退出码 0 |
| `pip wheel --no-deps --no-build-isolation .` | 构建成功 |
| 安装 wheel 到隔离目录后导入并匹配真实格式告警 | 成功返回 KB-003；JSON 与 JS 资源均入包 |
| `git diff --check` | 退出码 0 |

整仓结果包含 15 项跳过，不是零跳过全验收；没有新做双 Linux 部署、十万并发、
真实设备故障验证或独立留出集验收。Windows 补充验证范围见下节，不将其当作全量 Windows 验收。

## 纠正与复核

第一轮严格解析不接受真实告警结尾的 `<= red threshold 40.00`，造成前端 2 项
失败；已支持明确格式的阈值后缀并新增回归，不接受任意垃圾后缀。

只读复核指出重复同名特征时前后端取值不同。最终统一为最后一个有效测量值，
随后排除零风险，并增加两端正→零、零→正回归。前端也拒绝负窗口位置。
第一轮整仓 2090 passed/15 skipped 发生在最终改动之前，**最终采用上表 2092 结果**。

未暂存、未提交、未推送、未创建 PR、未合并。后续远端任务记录和人工评审仍需完成。


## Windows 隔离预览补充验证

2026-11-17 在用户 Windows 电脑从冻结基线建立独立副本，应用经清单校验的增补包，
使用既有 pp03（Python 3.12.15）和独立运行数据目录启动 3 设备、120 秒仿真。
未覆盖原仓库、冻结分支或旧数据，通知令牌未传入预览进程。

初次知识库单元测试为 16 passed / 1 failed，原因是测试读取生成的中文 JS 时依赖
Windows 默认 GBK 编码。显式指定 UTF-8 后，17 passed（0.19 秒）；产品执行代码未改。
该修正同步回云端源文件。Windows 未跑整仓测试。

在 Tabbit 中实际打开维护建议页，已确认 LIVE-001 历史红色告警对应 KB-001、
KB-003、KB-004，显示知识版本 2026-11-17.1、来源 G15-EXAMPLE-001、课程示例
与人工确认提示，原始告警包含 health 6.02、seq 29184 及 mean/rms/ptp 扣分特征。
仿真共采集 91,392 个样本、写入 357 个窗口。120 秒采集结束后的“持续状态已过期”
表示当前页面查看已保存的历史证据，不表示仍持续采集，也不作为服务故障结论。
本次只核验页面显示，没有执行设备维护、确认告警或任何真实外部通知。

UTF-8 测试修正同步并格式化后，云端完整复跑 2092 passed / 15 skipped（174.46 秒），
Black 128 文件通过，git diff --check 通过。上表已更新至此次最终结果。


## 只读目录页面增补

用户确认后增加 /knowledge 只读目录、控制台导航入口和维护建议的条目链接，
页面显示全部 8 条规则、适用条件、建议、依据、来源、版本和范围，无编辑入口。
云端知识库单测 + HTTP 静态资源集成测试 103 passed（3.34 秒），
Windows 同组测试 100 passed / 3 skipped（6.76 秒）；跳过项为平台限制。
完整云端最终回归 2092 passed / 15 skipped（174.46 秒），Node 135 passed。

在 Windows Tabbit 实际打开完整目录并点击 KB-003 索引，确认 URL 锚点、条目
高亮以及条件、建议、来源展示。没有进行手机尺寸或屏幕阅读器验证。
8 个相关代码与测试文件已在云端和 Windows 预览副本之间按规范化换行后的 SHA-256 对照一致。


## 界面文案精简与 PR 准备

按用户截图，仅移除知识库页黄色提示框与建议卡来源句尾的提示文案；
知识 JSON、来源元数据、条目内容和安全边界文档未删除。建议卡仍显示来源名称，
规则目录仍显示完整来源。此次仅改 HTML/JavaScript 展示，Node 最终 136 passed，
git diff --check 通过；上表 Python 全量结果对应相同后端代码，此次未重复全量 Python。

Windows 预览刷新后已读到精简来源句。知识库页面 HTML 已移除黄色框，继续视觉
核对时检测到用户正在其他窗口输入，因此没有抢占焦点。此前完整目录与条目定位
已做视觉验证，本次黄色框移除由前端测试核对。

2026-11-17 用户授权提交、推送独立分支并创建 PR，不授权合并。远端 main 已到
a7adcd3，较开发基线仅新增 final-delivery 文档，不与本次 23 个文件重叠。
至少一名真实审核者批准及 CI 结果仍是合并前条件，本文不宣称已满足。
