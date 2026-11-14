# #56 候选历史与工程冻结执行入口

2026-11-15，A 按[范围决定与事前冻结回执](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56#issuecomment-6881950509)停止 #56→#9 原独立流程，转为单独授权的工程验证收尾。原独立验收条件未满足，不再等待补齐后继续本批次。工程范围与实际结果见[工程验证记录](engineering_validation_20261115.md)。

原选择方法的[批准收据](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56#issuecomment-6881148970)、旧 baseline 和三事件 pending 账本原样保留：只发生 init、draw_intent、draw_returned，返回候选 4065886415；没有补造本人声明、接纳或第二次 RNG 调用。该候选进入工程数据生成后，不能在原批次补声明追认为未接触的独立集。方法批准、工具测试、工程运行和独立验收分别记录，不产生其他成员的本人声明或非作者批准。

## 1. 抽取前固定来源（historical，原独立方法）

本节与 §2 保留原批准方法及工具用法，供复核历史账本；不是继续抽取、声明或接纳的执行指令。

先固定实际拟抽取提交，对该提交全部 Git blob 重算已暴露值，记录逐值来源、所有未知项和审计文件 SHA-256。源码未知必须清零；仓外接触仍须本人声明。基于 `457c2612` 的静态审计覆盖 433 个 blob、141 个 Python 文件，已确认禁止集合为 31 值；这只是该提交的记录，不能直接替代新增工具集成后的审计。

`tools/holdout_selection.py` 使用一个保存在仓库外的 SQLite 事件账本。初始化 JSON 必须包含：

| 字段 | 含义 |
|---|---|
| `source_commit` | 实际拟抽取源码的完整 40 位 SHA |
| `method_blob` | 已批准选择方法文件的 Git blob |
| `audit_sha256` | 人工核对过的来源审计原字节 SHA-256 |
| `approval_receipt` | 真实方法批准的 HTTPS 收据 |
| `required_declarants` | B2、C1、实际执行方及全部生成者、保管者 |
| `excluded_seeds` | 审计确认的规范整数排除集合 |
| `unresolved_sources` | 必须为空；未知不能作为无接触 |

初始化会读取实际审计原字节，核对摘要、源码版本、未知项和完整排除集；抽取前还会现场核对 Git HEAD 及工作树干净状态。执行者负责核实审计推理与真实收据；一致性校验不是远端身份认证。

```sh
python tools/holdout_selection.py /outside-repository/selection.sqlite init --config /outside-repository/selection-config.json --audit /outside-repository/audit.json
python tools/holdout_selection.py /outside-repository/selection.sqlite status
```

## 2. 一次抽取与等待（historical，原独立方法）

```sh
python tools/holdout_selection.py /outside-repository/selection.sqlite draw --source . --audit /outside-repository/audit.json
```

该命令先提交 `draw_intent`，才调用操作系统 CSPRNG；返回后提交 `draw_returned`。两个槽位分别最多三次，返回是唯一预算计数。进程中断、返回未登记、版本不符、账本损坏或第三次否决后，必须暂停核对。不得新建另一账本、换进程或换批次清零。状态不明时保留原文件和输出，不自动重抽。

候选待声明时，全体槽位停止抽取。操作员取得并核实本人真实收据后，分别执行 `declare --slot SLOT --attempt ATTEMPT --seed VALUE --actor ROLE --contacted yes|no --receipt HTTPS_URL`。全部无接触声明齐备后，A 的真实接纳收据通过 `accept` 登记；这些命令不会代签或获取审批。`reject` / `revoke` 另要求 `--reason`，且不返还预算。后续接触披露不会被一条新的否认覆盖为有效；必须保留原披露和裁定。

每次操作保存 `status` 输出和 `tail_hash` 到外部证据。应用内 UPDATE/DELETE/同序号替换受阻，事件哈希链可复算；有文件/DDL 权限者仍可替换整个账本或删除尾部，不能声称文件级防篡改。外部尾锚与备份须持续保留。

## 3. 工程验证的精确配置与首次运行

原独立验收顺序保留于[test_plan §9](test_plan.md#9-独立保留集历史计划与范围处置)，其本人声明、精确配置复评和独立冻结条件未完成。本次工程批次另用事前授权与外置冻结 manifest：4065886415 加公开开发对照 42，各含 normal、degradation、spike、step、burst，共 10 配置、6 个应检事件。工程 slot 1/2 仅为矩阵编号，42 不是原 selector 的第二个候选或第二次抽取。

先固定源码 SHA、runner、10 份配置、consumer 和 health_rules 的摘要，再生成数据。运行中不改评分、阈值、配置、验证器或预期，不择优重跑或删除失败。旧 `docs/adr/0006-independent-validation-baseline.json` 的 `proposed_unfrozen`、null/false 保持原义，与本次工程 manifest 分开。

`tools/run_final_validation.py` 是冻结后工程证据入口，不自动授予独立性。它要求外置 manifest、完整源码 SHA、精确配置和规则 SHA-256、真实授权收据、`rules_frozen=true`，并且显式提供 `--execute`。具体格式和参数以 `--help` 与工具校验为准。没有 `--execute` 时只检查前置。

```sh
python tools/run_final_validation.py --source . --manifest /outside-repository/frozen.json --output /outside-repository/new-evidence
```

配置沿用 schema `1.0`：正常/退化用 `scenario.kind=normal/degradation`，故障由真实事件产生 Manifest 类型，不在 YAML 声明 `kind=fault`。每槽五种轮廓，真实独立 CLI 执行 validate→simulate→consume→report；正常零告警、故障 TP=6/FN=0、退化后半健康分中位数低于前半，故障 FP/redundant/unmatched 分列且不追加零误报门槛。保留每个命令的 stdout、stderr、退出码、报告及失败。

实际运行的证据固定标为 `engineering_validation`、`independent_acceptance=false`。原独立流程已停止且条件未满足，不能因为该工具退出 0、CI 绿或项目事项处置就改成通过。Gate 0 停修未通过、AND=false、100k 未验证的结论独立保留。
