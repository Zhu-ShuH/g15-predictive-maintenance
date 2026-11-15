# G15 预测性维护场景沙盘

本项目用可复现的 NumPy 仿真生成振动/温度数据，经 NPZ + Manifest 文件接口交给独立消费者，完成特征、健康评分、告警、SQLite 持久化和 JSON/Markdown 报告。它是教学沙盘，不是已在真实设备上验证的诊断或自动控制系统。

> **收尾范围（2026-11-15）**：`validate → simulate → consume → report` 主链、`benchmark`、`reset-alarms`、实时仿真、设备风险注入、控制台与通知功能均已进入 main。`g15 verify` / `g15 doctor` 两个 Python 子命令仍未接线；Shell 入口 `scripts/verify.sh` 已实现质量、测试、配置和闭环门禁。#56→#9 原独立流程按 [A 的范围决定](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56#issuecomment-6881950509)停止，原独立验收条件未满足；工程验证单独留证。#135 只读授权查询已交付。#136 Gate 0 已执行但未通过，已停止修复并关闭留档，100k 未验证。

**正式交付准备 #177 / PR #178**：基线为 `1cc3d5f2b9ea0b6f5ceb969a2637844feeb410e1`，受测产品提交为 `16d57df485d428c02914bac372b61c67917857da`。双Linux新克隆验收及三轮百万点均已通过，禁网范围分别说明；正文见[交付报告](docs/final_delivery_report.md)，原始结果见[本轮实测](docs/delivery_validation_20261115.md)。新PR仍待最终head真实非作者批准；成员/教师事项见[交付清单](docs/delivery_readiness_20261115.md)。本轮不重启原独立批次或100k容量工作。

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

## 网页控制台（本机只读）

控制台提供设备选择、真实健康趋势、历史告警筛选、只读维护建议和同一运行的分析报告。前端通过同源本机网关调用现有认证HTTP查询，Bearer仅由服务端读取，不进入浏览器。窗口趋势可逐页或沿真实cursor顺序加载全程，报告中的未判定和误报结果保留原义。维护建议关联来源规则、活动状态、告警窗口及原因中的扣分特征，只辅助人工检查，不生成故障诊断、工单或设备控制动作。

新增独立入口为 `python -m g15.console`；原CLI、评分公式、认证和分页保持不变，五维诊断仅在窗口结果中新增兼容旧数据的只读 `risk_dimensions` 投影。需提供已登记的运行目录、目录JSON、正在运行的查询服务以及服务端token文件。完整启动、身份边界、API与本地演示步骤见[控制台说明](docs/console.md)。默认只绑定127.0.0.1，不是远程多用户登录方案，也不代表Gate0通过。

同一服务提供 `/screen` 大屏驾驶舱、`/operations` 运营看板和 `/diagnosis` 单设备诊断工作台，可与设备控制台互相跳转；所有页面共享用户指定配色的 Cyber Wafuu 主题、毛玻璃组件和 JRPG 按钮。大屏包含真实健康分布、设备矩阵、所选设备趋势、维护建议和持续采集状态；运营看板包含状态指标卡、趋势及可搜索设备表；诊断页展示已存健康分、最新窗口的五维风险分解与告警证据链。各页复用现有查询，不伪造租户、方案卡、工单、五维工业诊断或真实设备联网数据。

## 已有证据与未完成项

| 范围 | 当前事实 | 证据边界 |
|---|---|---|
| 主链实现 | 配置、仿真、故障、生产、消费、分析、存储、报告已进入 main | 实现完成不等于最终验收完成 |
| 质量门禁 | Black、Pylint `>=8.5`、pytest、配置校验与 G4 闭环由 `verify.sh` 编排 | 每个候选发布 SHA 仍须重新执行并保存退出码 |
| 百万点 | Ubuntu/RHEL 已有 1,000,000 点、10 分片、3 轮/系统的真实基准证据 | 只证明冻结配置和对应 SHA；不证明工业准确率 |
| 双 Linux 闭环 | `545a598c` 已在两机公开克隆执行 setup→clean→demo→verify→benchmark，均退出 0 | 不能替代 #56/#9 的独立保留集检出率 |
| 故障恢复 | 坏分片/SHA 拒收、Linux `kill -9` 重放与五表对账已有测试和双机记录 | 不宣称断电零丢失，也不宣称 soak 已完成 |
| 独立验收 | #56→#9 原独立范围按决定停止，旧基线与三事件 pending 账本保留 | 原独立条件未满足；不补签本人声明，不把事项处置记作通过 |
| 工程验证 | 4065886415 与公开开发对照 42，各五种固定轮廓；单独授权、先冻结再执行 | 原始结果见[本轮工程验证](docs/engineering_validation_20261115.md)；`independent_acceptance=false`，不替代双 Linux 或独立验收 |
| 容量与评分材料 | #142 材料已交付；#136 Gate 0 已执行、未通过并停止修复 | 原失败、AND=false 保留；100k 未验证，不把低负载结果外推为容量达标 |

实测数字、环境和原始边界见 [测试报告](docs/test_report.md)，计划与现场用例见 [测试计划](docs/test_plan.md)。

项目交付入口、#56 工具实施与保留边界集中见[收尾状态](docs/final_closeout.md)；本轮范围、冻结记录与实际结果见[工程验证记录](docs/engineering_validation_20261115.md)。

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
- [正式交付清单](docs/delivery_readiness_20261115.md)：七阶段/四件套、真实执行、审批与个人/教师条件。

## 开发与合并

遵循 Issue → feature/fix/test/docs 分支 → PR → 至少 1 位非作者 Approve → CI → 合并；禁止直接向 `main` 推送。质量命令：

历史 #175/#176 无正式Review，A的接收不是教师豁免；全仓审批分类、其他整包覆盖路径和先合后审记录见[合规说明](docs/compliance.md#6-177-审批贡献与范围确认)。补审不倒填历史，本轮新PR必须真实非作者对最终head Approve且CI通过后合并。下面是检查命令，不表示本页已运行：

```sh
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5
python -m pytest -q
```

仓库目前尚未提供许可证；在对外复用、分发或申报“被其他组复用”加分前，必须由 A 明确许可证与授权范围。


## 十台仿真设备持续采集与分析

独立入口 `python -m g15.live` 按墙钟持续生成新样本，接现有评分、告警、SQLite和认证HTTP查询，并驱动控制台自动更新。默认10台、每台256Hz、256点窗口，设备状态跨批次延续，队列和资源有界。当前是公开仿真源，不接真实硬件，也不代表Gate0通过。启动、停止、独立live协议和实测边界见[持续采集说明](docs/live.md)。
