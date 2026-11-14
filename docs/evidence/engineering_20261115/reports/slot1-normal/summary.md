# 验证报告：20261114T180743Z-engineering_20261115_slot1_normal-b25038

> 本文件由 `g15.reporting.builder` 从运行目录与 SQLite 数据库回读生成；
> 检出率口径见 `docs/interface.md` §8.5 与 `docs/acceptance.md` §3。
> `null` 表示该指标不存在或不可判定，不代表 0，也不代表 100%。

## 1. 结论

- 场景类型：`normal`
- 类型来源：`manifest`
- 类型适用范围：manifest.json scenario_kind declares the kind (schema 2.0)
- 可验证：**是**
- 判定：**通过**

## 2. 关键指标

| 指标 | 值 |
|---|---|
| 注入事件总数 | 0 |
| 已检出事件 | 0 |
| 漏报事件 | 0 |
| 检出率 | null |
| 误报数 | 0 |
| 冗余告警数 | 0 |
| 未匹配告警数 | 0 |
| 参与匹配的告警数 | 0 |
| 被丢弃的告警行（重复键/非法等级） | 0 |
| 匹配允许的最大检测时延（窗口数） | 2 |
| 时延取值来源 | `metrics.MAX_DETECTION_LAG_WINDOWS_DEFAULT` |

## 3. 运行口径

| 项 | 值 |
|---|---|
| run_id | `20261114T180743Z-engineering_20261115_slot1_normal-b25038` |
| scenario_id | `engineering_20261115_slot1_normal` |
| device_id | `DEV-001` |
| schema_version | `2.0` |
| scoring_version | `heuristic-v1` |
| analysis_window_points（从已存窗口读出） | 256 |
| total_points | 65536 |
| scalar_value_count | 131072 |
| processed_points（已提交片覆盖） | 65536 |
| analysed_points（真实进入窗口） | 65536 |
| dropped_points（片内未成窗） | 0 |
| rejected_points（未消费的片） | 0 |
| pending_parts（无处理记录的片） | 无 |
| digest_mismatch_parts（摘要与清单不符的片） | 无 |
| raw_feature_windows（库中全部窗口行） | 256 |
| excluded_feature_windows（不属于有效片的窗口行） | 0 |
| raw_alarm_count（库中全部告警行） | 0 |
| excluded_alarm_count（不属于有效片的告警行） | 0 |
| feature_windows | 256 |
| health_score_min | 98.17874434309708 |
| health_score_max | 99.99927508968764 |
| elapsed_seconds（生产计时窗口） | 0.136383 |

## 4. 逐事件匹配

本运行的真值文件明确记录零个注入事件。

## 5. 错误记录

运行日志中没有 ERROR 级记录。

## 6. 运行环境

| 项 | 值 |
|---|---|
| Python | 3.12.15 (CPython) |
| NumPy | 2.5.4 |
| 平台 | Windows-11-10.0.22631-SP0 |
| 架构 | AMD64 |

> 本报告是单次运行的可复核证据，不是双 Linux 部署验收记录；
> 正式验收须在 Ubuntu 24.04 与 RHEL 9 上按 `docs/acceptance.md` §1 另行留证。
