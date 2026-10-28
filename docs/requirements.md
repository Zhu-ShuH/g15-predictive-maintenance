# G15 软件需求与验收追踪

> Issue [#2](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/2)；负责人 A；状态核对日期：2026-10-28。实现快照为 main `75b119454a1ac075620bdcc74826730fa7b4ac02`。本次更新追踪关系，不改变验收标准、公共接口、规则或 seed，不关闭接口评审任务。

## 1. 来源、范围与判定方法

需求来自课程要求摘录、A 于 2026-10-16 确认的小组默认方案及 [验收规范](acceptance.md)。原始教师材料不随公开仓库提交；NPZ、SQLite、Ubuntu 24.04 LTS/RHEL 9 是小组工程选择，不能写成教师逐项指定。答辩日期未定，D1–D14 保留为相对排期模板。

正式环境为 Conda `pp03` / Python 3.12，安装步骤见 [environment](environment.md)；Python 3.11 为额外兼容 CI。公开 GitHub 仓库、现场新克隆运行、双 Linux 分别留证等约束保持不变。

本页区分三类证据：**主线实现**说明已进入上述提交；**候选交付**说明仍在 PR 中；**正式验收**须按同一集成版本及规定环境实际执行。测试文件存在、单次 Windows 结果或 CI 绿均不能独自证明整项需求通过。

## 2. 用户故事、实现证据与剩余缺口

| ID | 用户故事与可验收结果 | 主线实现与可核对入口 | 尚待完成 / 负责人 |
|---|---|---|---|
| R01 | 演示者能运行正常、缓慢退化、突发故障三类预设 | 配置 [#26](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/26)、仿真 [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15)、故障 [#27](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/27)、生产 [#30](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/30)；见 `test_config.py`、`test_faults.py`、`test_producer.py` | C3/B1（承接原 C5）完成 CLI 与三场景完整链报告；已有 Python 模块不等于演示链验收完成 |
| R02 | 操作员只改 YAML 即可改变退化率和故障参数，无效输入被拒绝 | `config.py`、`configs/` 与配置/仿真/故障单测；采样率上界修复 [#47](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/47)、[#55](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/55) | C3/B1/C1/C5 在最终集成版本留下配置 A/B、源码未变、输出变化及非法配置命令记录 |
| R03 | 测试者能按配置、seed、源码和依赖版本复现测量 | 仿真确定性及噪声黄金值 [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15)、[#33](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/33)；共享类型/RNG [#31](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/31) | A/C6 的依赖锁定 [#28](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/28) 与跨环境复现；run_id/报告时间不作为测量数组相等条件 |
| R04 | 评审者可核对完整 1,000,000 点生产严格小于 60 秒 | 真实生产入口及 Windows 三次原始证据 [#53](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/53)、[ADR-0001](adr/0001-simulation-model-and-determinism.md) | C6 执行双 Linux 各三次并补进程外内存与计时范围证据，B1（承接原 C5）汇总；不重列已交 Windows 证据；[#51](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/51) 的口径补充仍 open |
| R05 | 下游在独立进程读公开文件，不依赖仿真内存状态 | 序列化 [#29](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/29)、生产 [#30](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/30)、消费 [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64)；`test_serialization.py`/`test_consumer.py` | C3 的消费 CLI [#71](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/71) 仍为候选；simulate 与 consume 分进程命令须在集成后留证 |
| R06 | 维护人员看到特征、0–100 健康分与可解释告警 | 分析 [#54](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/54)、消费/SQLite [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64)；`test_features.py`、`test_health.py`、`test_alarms.py` | B2 的数值边界修复 [#73](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73) 已提交整改候选 `5314f905e92adb3cfcdd03dde6d20463b138cc30`，A已复审通过，仍待B1/C4/C2相关审查及集成；契约 [#57](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57) 与报告展示待闭环 |
| R07 | 评审者能独立核对受控事件检出、漏报、误报和延迟 | 验收规范与 [ADR-0006](adr/0006-independent-validation-protocol.md) 已有审阅方案；主线 reporting 仍为空模块 | 验证/报告候选 [#70](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70) 未合入；[#56](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56) 的独立数据方案/规则冻结与正式执行未完成 |
| R08 | 项目负责人获取可追溯 JSON/Markdown 闭环报告 | 主线已有 Manifest 和 SQLite 上游；`docs/test_report.md` 仍占位 | 当前 #9 报告实现由 B1 接手，候选为 [#70](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70)；集成后由相关实现/验证角色留版本、点数、耗时及错误证据 |
| R09 | 运维人员重复消费或中断重启后不重不漏 | [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64) 提供消费、存储、状态恢复与幂等单测；[ADR-0005](adr/0005-analysis-persistence-contract.md) 记录契约提案 | C2/C6/C5 补真实 Linux `kill -9` 与不中断对照；不能把单元回滚当进程强杀或断电测试 |
| R10 | 首次接手者在两种 Linux 同一版本新克隆安装运行 | [environment](environment.md) 有环境约定；[#59](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/59) 仅修复 shell 文件可执行位 | C6/A 完成 [#10](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/10)、#28；当前 setup/demo/verify/benchmark 仍为失败退出的占位脚本 |

表内测试文件均位于 `tests/unit/`，只标识可检查入口，不声称本次重新运行。

## 3. 非功能需求与统计口径

| ID | 保持的要求 | 当前证据边界与补齐方式 |
|---|---|---|
| N01 | 1,000,000 个采样时刻；生成、序列化、哈希和 complete Manifest 发布合计严格 <60 秒；双信号共 2,000,000 标量另报 | 沿用 acceptance §2，双 Linux 各三次；#53 的 Windows 结果不替代 Linux，不把内核计时或事后校验混进生产窗口 |
| N02 | 预设受控集事件召回 100%，同时报告误报和延迟；零事件召回为 null | 保持 acceptance §3；开发已查看数据与独立保留验证分开，#56 未冻结前不选/跑新保留 seed |
| N03 | 配置/seed/版本可追溯；UTC、绝对序号和单位明确 | 保留仿真确定性；最终记录源码、配置/规则摘要与依赖，不以当前机器时钟生成测量 |
| N04 | 参数/配置 2，I/O/Schema 3，实际验收失败 4，未知错误 5，成功 0 | 沿用 interface §10；主线 validate 的 0/2 已实现，未知/占位分支使用 5；cli.py 现只有 EXIT_OK/EXIT_INVALID/EXIT_UNEXPECTED，3/4 常量尚未定义。#71 候选定义 consume 的 3，验收失败 4 随后续 verify/验收入口接线补齐；#70 提供报告能力，不等于 CLI 已接线 |
| N05 | 安装可联网，业务运行不依赖网络或在线 AI | C6 实测现场网络、运行离线与备用依赖；尚未完成的离线安装准备保持待办 |
| N06 | 数据、告警状态、分片游标同事务；重复/恢复可诊断 | #64 已有实现与回归；真实强杀、日志和重启后内容一致性仍需实测，不宣称断电零损失 |
| N07 | 至少 15 单测、20 个可执行用例、3 类故障注入证据 | 测试存在及数量不替代执行结果；三类为业务故障、文件损坏、Linux 强杀恢复，不能用三种波形替代三类可靠性证据 |
| N08 | Ubuntu 24.04 LTS 与 RHEL 9 同一集成提交分别安装运行留证 | CI 的 Python 3.11/3.12 矩阵不等于两种 Linux；当前不声明此项已通过 |

旧文中 main `2c14759` 的“收集 88 个用例”只适用于早期快照，不作为当前统计。本次不引入新的通过总数；每次结果须绑定自己的源码 SHA、环境、命令、退出码与跳过原因。A 的历史实测归档见 [#75](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75)、[#78](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78)，两者在本快照仍待审合入，且不是正式验收报告。

## 4. 未合入候选与变更控制

| 候选 | 已交付的对象 | 保留的条件 |
|---|---|---|
| [#67](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/67) | 持久化、校验分层与 CLI 接线的接口正文 | 成员实际审阅与 §13 兼容证据；本页不提前冻结候选 |
| [#70](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70) | 独立验证器与闭环报告 | B2提出场景类型依赖名称的风险，A独立复证并请求修改；B1须提交判据来源/未知类型方案及回归，其他集成与独立验收条件保留 |
| [#71](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/71) | consume CLI 接线 | C2 对公开签名/异常集合的本人确认等门禁 |
| [#72](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72) | 原始分片独立校验 | R5 已获 A 与 B2 本人复审 APPROVED（B2：5684239091）；C4/C3 与相关接口条件仍保留；七项检查不核绝对时间公式 |
| [#73](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73) | 特征数值稳定性修复 | `5314f905e92adb3cfcdd03dde6d20463b138cc30` 已提交类型、整数精度、FEATURES_VERSION 1.1 与兼容说明整改；A已对上述候选复审APPROVED，仍待B1本人复审旧CR及C4/C2意见；未合入不当作主线已解决 |
| [#76](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/76) | 多片末片上界预检 | 评审/集成前置；当前主线尚无此预检，不用文档更新替代代码合入 |

公共接口按 [interface §13](interface.md#13-接口变更流程) 处理；最终成绩按 [acceptance](acceptance.md) 执行。A 汇总实际意见和缺口，模块负责人补实物，不能互相代签。#17 的共享类型迁移已经由 #31 合入，不能继续列为未实施；#2/#57 仍保留当前持久化等接口收口任务。
