# G15 收尾状态与交付入口

2026-11-15：A 按已确认的工程验证范围完成交付收尾。**工程矩阵10/10通过、事件6/6检出；原独立验收未满足。** [范围决定](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56#issuecomment-6881950509)在数据生成前记录；#56/#9 的原独立目标停止并按 not_planned 关闭，关闭状态不代表原终验通过。

| 项目 | 最终事实 | 交付或证据入口 |
|---|---|---|
| 产品主链 | YAML→仿真→NPZ/Manifest→消费→健康分/告警→SQLite→报告已交付 | [README](../README.md)、[接口](interface.md)、[现场手册](acceptance_runbook.md) |
| 实时产品 | 实时仿真、无时限采集、设备风险控制、控制台及通知实现已接收至主线；无时限仍受磁盘保护和反压限制 | [实时仿真](live.md)、[控制台](console.md) |
| #56 实施 | PR #175 交付持久选择账本、固定源码审计绑定及冻结执行工具；原选择流程停止在三事件/pending，未伪造声明 | [执行说明](holdout_execution.md)、[原基线](adr/0006-independent-validation-baseline.json) |
| 工程验证 | 源码0292cc04，4065886415+公开对照42，10配置/40命令成功、TP6/FN0；正常零告警，退化各1告警完整列出 | [冻结、逐例、逐事件证据](engineering_validation_20261115.md) |
| #9 独立验收 | 报告实现和工程结果已交付；原独立验收未满足，independent_acceptance=false | [测试报告](test_report.md)、[原独立协议](adr/0006-independent-validation-protocol.md) |
| 质量 | 被测源码CI806四job成功，Python3.11/3.12各2048 passed/15 skipped；文档收尾PR另有自身CI | [CI806](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43623695752) |
| 容量 | Gate0 FAILED、AND=false，原9/新26失败保留；停止修复与重测，#136关闭留档；100k未验证 | [测试报告](test_report.md) §10 |
| 双系统与现场 | Ubuntu/RHEL已有历史主链、质量、故障恢复和百万点证据；最终候选整包复证、教师注入、个人讲解未完成 | 按具体SHA和环境使用历史证据，不预先标通过 |

## 保存与使用

源码、报告和部署入口可按README与现场手册交付；本次收尾不代替课程/教师验收。未来恢复独立验证、容量或现场工作须另立受控范围，不把已停止事项留成默认后台待办。

测试及完整运行目录、SQLite、原始NPZ、pytest缓存和临时目录始终位于Obsidian规划库之外。output只保存普通用户可读的摘要与证据。公开材料不含个人机器路径、凭据、原始数据库或大型数据。

自动提醒保持关闭；本次未发送真实通知或操作真实设备。原独立候选4065886415已用于工程运行，不得补声明后冒充未接触独立集；原账本和失败证据完整保留。
