# G15 预测性维护场景沙盘

本项目用可复现的 NumPy 仿真生成振动/温度数据，经 NPZ + Manifest 文件接口交给独立消费者，完成特征、健康评分、告警、SQLite 持久化和 JSON/Markdown 报告。它是教学沙盘，不是已在真实设备上验证的诊断或自动控制系统。

> **当前基线（2026-11-10，`main@997acaeb`）**：`validate → simulate → consume → report` 主链、`benchmark`、`reset-alarms` 与部署脚本均已进入 main。`g15 verify` / `g15 doctor` 两个 Python 子命令仍未接线；Shell 入口 `scripts/verify.sh` 已实现质量、测试、配置和闭环门禁。最终独立保留集验收仍按 #56 → #9 执行；查询/容量增量仍按 #135 → #136 执行，不能把计划写成已通过。

## 从全新克隆开始

目标环境为 Ubuntu 24.04 LTS 或 RHEL 9，Conda `pp03` / Python 3.12。安装阶段需要取得锁定依赖；安装完成后，运行阶段不依赖网络、云端 AI 或外部服务。

```sh
git clone https://github.com/Zhu-ShuH/g15-predictive-maintenance.git
cd g15-predictive-maintenance
bash scripts/setup.sh
bash scripts/clean.sh
bash scripts/demo.sh
bash scripts/verify.sh
bash scripts/benchmark.sh
```

五条命令任一非零都表示该轮未通过，不得只截取成功步骤。`demo.sh` 要求 `runs/` 为空；`clean.sh` 只清理封闭清单内的可重建产物。完整环境与退出码说明见 [部署说明](docs/deployment.md) 和 [运维说明](docs/operations.md)。

不用脚本时，最短业务链为：

```sh
conda run --no-capture-output -n pp03 python -m g15 validate --config configs/sudden_fault.yaml
conda run --no-capture-output -n pp03 python -m g15 simulate --config configs/sudden_fault.yaml --output-root runs
conda run --no-capture-output -n pp03 python -m g15 consume --run-dir runs/<run_id> --config configs/consumer.yaml
conda run --no-capture-output -n pp03 python -m g15 report --run-dir runs/<run_id>
```

## 已有证据与未完成项

| 范围 | 当前事实 | 证据边界 |
|---|---|---|
| 主链实现 | 配置、仿真、故障、生产、消费、分析、存储、报告已进入 main | 实现完成不等于最终验收完成 |
| 质量门禁 | Black、Pylint `>=8.5`、pytest、配置校验与 G4 闭环由 `verify.sh` 编排 | 每个候选发布 SHA 仍须重新执行并保存退出码 |
| 百万点 | Ubuntu/RHEL 已有 1,000,000 点、10 分片、3 轮/系统的真实基准证据 | 只证明冻结配置和对应 SHA；不证明工业准确率 |
| 双 Linux 闭环 | `545a598c` 已在两机公开克隆执行 setup→clean→demo→verify→benchmark，均退出 0 | 不能替代 #56/#9 的独立保留集检出率 |
| 故障恢复 | 坏分片/SHA 拒收、Linux `kill -9` 重放与五表对账已有测试和双机记录 | 不宣称断电零丢失，也不宣称 soak 已完成 |
| 当前开放验收 | #56、#9、#135、#136 及期末标准对齐 #142 | 100k、p95/EXPLAIN、独立保留集未执行时必须写“待执行” |

实测数字、环境和原始边界见 [测试报告](docs/test_report.md)，计划与现场用例见 [测试计划](docs/test_plan.md)。

## 安全边界

- **不存密码**：当前主链没有账号登录或密码数据库，不得为了演示把明文密码写进 YAML、源码、日志或仓库。未来若引入口令，必须先走契约评审并使用带随机盐的 PBKDF2/等价强哈希与恒时比较。
- **数据不出域**：运行数据默认只保存在本机 `runs/`；运行阶段不联网。对外发出的报告、日志和截图必须先做脱敏并取得授权，禁止包含 token、密码、真实个人身份、真实厂区信息、私人绝对路径或 `private_truth` 原文。
- **无设备执行器**：系统只产生告警，不下发停机、维修或控制命令。`reset-alarms` 是显式运维动作，必须提供 `--yes` 和非空原因；未来真实设备接入必须另有变更单、旁路方案和两位非作者复核，任何停机动作必须人工确认。
- **审计诚实性**：现有 reset 历史可追溯，但独立、不可 UPDATE/DELETE 的 append-only 安全审计仍需单独 Issue/ADR 落地；在此之前不得宣称满足该红线。

## 文档入口

- [验收规范](docs/acceptance.md)与[期末评分/红线追踪](docs/compliance.md)：什么算通过、什么仍待执行。
- [数据治理与安全边界](docs/data-governance.md)：数据分级、脱敏授权、凭据、真实设备和人工确认规则。
- [需求规格](docs/requirements.md)、[架构设计](docs/architecture.md)、[接口契约](docs/interface.md)。
- [测试计划](docs/test_plan.md)、[测试报告](docs/test_report.md)。
- [部署说明](docs/deployment.md)、[运行手册](docs/operations.md)、[新人上手](docs/onboarding.md)。
- [答辩材料](docs/presentation.md)、[复盘](docs/retrospective.md)、[贡献记录](docs/contributions.md)。

## 开发与合并

遵循 Issue → feature/fix/test/docs 分支 → PR → 至少 1 位非作者 Approve → CI → 合并；禁止直接向 `main` 推送。质量命令：

```sh
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5
python -m pytest -q
```

仓库目前尚未提供许可证；在对外复用、分发或申报“被其他组复用”加分前，必须由 A 明确许可证与授权范围。
