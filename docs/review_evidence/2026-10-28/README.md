# A 侧固定版本评审实测证据

这些记录来自 2026-10-26 至 2026-10-28 的既有 A 侧审阅。每组绑定完整提交 SHA；本次上传整理既有日志与诊断，未重跑测试。结果描述对应候选，不代表 main 已集成或项目验收完成。

| 候选 | A 实测全量 | 对应记录 |
|---|---|---|
| [#70 `144f7ae2`](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70#pullrequestreview-5650335018) | 581 passed / 9 skipped; exit 0 | [pr70-144f7ae](pr70-144f7ae/metadata.json) |
| [#71 `6c146109`](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/71#pullrequestreview-5668822279) | 542 passed / 8 skipped; exit 0 | [pr71-6c146109](pr71-6c146109/metadata.json) |
| [#72 `abc6dfa4`](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5668814813) | 548 passed / 9 skipped; exit 0 | [pr72-abc6dfa4](pr72-abc6dfa4/metadata.json) |
| [#72 `e97c2efa`](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5669551972) | 仅增量诊断，未跑全量 | [pr72-e97c2ef](pr72-e97c2ef/metadata.json) |
| [#72 `80e9f896`](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5670400729) | 569 passed / 9 skipped; exit 0 | [pr72-80e9f89](pr72-80e9f89/metadata.json) |
| [#73 `ca83aec3`](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5677021053) | 537 passed / 8 skipped; exit 0 | [pr73-ca83aec](pr73-ca83aec/metadata.json) |

## 如何核查

每组 metadata.json 给出完整 SHA、运行日期、环境、执行命令、跳过原因和实际验证范围。pytest.log 保留已采集的全量输出；JSON 为公开入口诊断结果。artifact-index.json 给出发布文件和本地原始字节的 SHA-256，便于核对脱敏前后的归档。

PR #72 的 abc6dfa4 记录故意保留“全量通过但诊断假通过/异常”的结果，e97c2ef 保留零维数组的 IndexError，80e9f89 给出后续修复。PR #73 的 timedelta、整数精度和普通幅值末位差异同样保留。它们是明示诊断 fixture，不是正式检出率成绩。

## 路径处理与可读性

本地绝对路径已替换成稳定标记，说明见 sanitization.json。原始本地证据保持不变。#70 原日志中的部分中文在采集时已出现替换字符，日志原样保留；可读的跳过分类是 6 项性能 opt-in、2 项符号链接权限、1 项 Windows 无 POSIX kill-9。

## 结果边界

所有运行均为 Windows / Python 3.11.9 / NumPy 2.3.5 / pytest 8.4.2。环境、跳过与耗时按各次真实记录引用。Black/Pylint 没有在这些 A 侧记录中另跑；远端 CI 属独立来源。#72 作者机器自报的配置失败未在 A 环境复现，不能据此确认其根因。

本归档不包括虚拟环境、源码候选副本、pytest 临时目录、生产运行数据或私人规划笔记。A 的自动代码复核不替代成员人工合并审批；接口冻结、双 Linux、真实 kill-9、独立保留集及正式性能/检出率仍按各自任务留证。

## 摘要字节与索引格式

校验摘要时读取 Git blob 的原始字节；例如通过 Python 的 `subprocess.check_output(["git", "show", "<commit>:<仓库内路径>"])` 取得 bytes 后计算 SHA-256。不要把终端显示或文本重定向后的内容当原始字节。Windows 的 `core.autocrlf=true` 可能把工作树 LF 转成 CRLF，直接对工作树算摘要会与 blob 不同；这不表示归档损坏。若使用工作树核验，应在全新检出前设置 `core.autocrlf=false`。

本目录索引是含 `artifacts` 数组的对象：`sha256` / `bytes` 指发布后 blob，`source_sha256` 指本地原始字节。历史归档 [PR #78](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78) 的索引直接为数组，对应字段为 `published_sha256` / `published_bytes` / `original_sha256`。批量读取时，先按顶层类型取数组（数组直接用，对象取 `artifacts`），再按上述映射读取。已有索引与证据字节保持原样，后续归档沿用 #78 的明确字段名，不把发布摘要当原始摘要。
