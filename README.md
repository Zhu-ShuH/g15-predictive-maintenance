# G15 预测性维护场景沙盘

本项目用可复现的 NumPy 仿真生成振动/温度数据，经 NPZ + Manifest 文件接口交给独立消费者，完成特征、健康评分、告警、SQLite 持久化和 JSON/Markdown 报告。它是教学沙盘，不是已在真实设备上验证的诊断或自动控制系统。

> **当前基线（2026-11-11，`main@a9951403`）**：`validate → simulate → consume → report` 主链、`benchmark`、`reset-alarms` 与部署脚本均已进入 main。`g15 verify` / `g15 doctor` 两个 Python 子命令仍未接线；Shell 入口 `scripts/verify.sh` 已实现质量、测试、配置和闭环门禁。最终独立保留集验收仍按 #56 → #9 执行；#135 只读授权查询已由 #149 合入；容量仍按 #136 Gate 0 执行，不能把计划写成已通过。

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

## 现场一键验收（Issue #144）

完整前置条件、证据判定、退出码和平台边界见
[现场一键验收运行手册](docs/acceptance_runbook.md)。

依赖已由 `scripts/setup.sh` 安装后，在仓库根运行：

```sh
bash scripts/acceptance.sh
```

入口不安装或下载依赖。它默认拒绝脏工作树，并用 `git archive <SHA>` 将固定提交复制到系统 `mktemp` 目录，在仓库外依次运行 `verify.sh`、离线/坏数据/`kill -9` 相关故障注入和 `benchmark.sh`；任一步非零，整体立即非零。源仓库的 `runs/` 不会被使用，测试临时目录也不会放入 Obsidian 规划库或其 `output/`。默认证据目录为系统临时目录下的 `g15-acceptance-<SHA>.*`，其中记录提交 SHA/tree、快照模式、系统与 Python/NumPy/PyYAML 版本、逐条原始退出码和日志。需要长期留档时，把根目录显式指向仓库和规划库之外：

```sh
mkdir -p "$HOME/g15-evidence"
G15_ACCEPTANCE_EVIDENCE_ROOT="$HOME/g15-evidence" bash scripts/acceptance.sh
```

只有开发未提交实现时才可显式设置 `G15_ACCEPTANCE_ALLOW_DIRTY=1`；这类运行会醒目标注 `formal_evidence_eligible=false`，只能用于预检，不能作为最终 SHA 证据。总入口把质量/测试/故障/性能门禁失败统一返回 4，把日志/验收框架故障返回 5；子命令原始退出码保留在 `steps.tsv`。

离线用例会先证明 DNS 与网络 socket 自检被测试专用守卫拒绝，再真实运行小型 `validate → simulate → consume → report`。该守卫只覆盖 Python `socket` 审计事件，**不等于操作系统已经断网**，也不能排除绕过 Python 审计 API 的原生代码；最终提交 SHA 仍须在关闭网卡或隔离网络命名空间后复跑同一入口并保存证据。Windows 没有 POSIX `SIGKILL`，该项会按既有用例明确 skip，正式 `kill -9` 证据必须来自 Linux。本入口不执行 #56 的未公开保留集；退出 0 也不等于“独立保留集检出已通过”。

## 已有证据与未完成项

| 范围 | 当前事实 | 证据边界 |
|---|---|---|
| 主链实现 | 配置、仿真、故障、生产、消费、分析、存储、报告已进入 main | 实现完成不等于最终验收完成 |
| 质量门禁 | Black、Pylint `>=8.5`、pytest、配置校验与 G4 闭环由 `verify.sh` 编排 | 每个候选发布 SHA 仍须重新执行并保存退出码 |
| 百万点 | Ubuntu/RHEL 已有 1,000,000 点、10 分片、3 轮/系统的真实基准证据 | 只证明冻结配置和对应 SHA；不证明工业准确率 |
| 双 Linux 闭环 | `545a598c` 已在两机公开克隆执行 setup→clean→demo→verify→benchmark，均退出 0 | 不能替代 #56/#9 的独立保留集检出率 |
| 故障恢复 | 坏分片/SHA 拒收、Linux `kill -9` 重放与五表对账已有测试和双机记录 | 不宣称断电零丢失，也不宣称 soak 已完成 |
| 当前开放验收 | #56、#9、#136（Gate 0）及本单 #142 | 100k、p95/EXPLAIN、独立保留集未执行时必须写“待执行” |

实测数字、环境和原始边界见 [测试报告](docs/test_report.md)，计划与现场用例见 [测试计划](docs/test_plan.md)。

## 安全边界

- **不存密码**：当前主链没有账号登录或密码数据库，不得为了演示把明文密码写进 YAML、源码、日志或仓库。未来若引入口令，必须先走契约评审并使用带随机盐的 PBKDF2/等价强哈希与恒时比较。
- **数据不出域**：运行数据默认只保存在本机 `runs/`；运行阶段不联网。对外发出的报告、日志和截图必须先做脱敏并取得授权，禁止包含 token、密码、真实个人身份、真实厂区信息、私人绝对路径或 `private_truth` 原文。
- **无设备执行器**：系统只产生告警，不下发停机、维修或控制命令。`reset-alarms` 是显式运维动作，必须提供 `--yes` 和非空原因；未来真实设备接入必须另有变更单、旁路方案和两位非作者复核，任何停机动作必须人工确认。
- **审计诚实性**：reset 历史与 append-only 审计链已由 #143/#147 落地，UPDATE/DELETE/REPLACE 被触发器拒绝；这是应用级只增，拥有文件/DDL 权限者仍可替换数据库，尾锚和尾缀删除不可检，不得宣称文件级防篡改。

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
