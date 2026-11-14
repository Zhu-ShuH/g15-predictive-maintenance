# 2026-11-15 工程验证收尾证据

A 完成一次事前冻结的工程验证：**10/10 场景通过，40/40 CLI 命令退出 0，受控事件 TP=6 / FN=0**。本批 `independent_acceptance=false`，原 #56/#9 独立验收未满足；这是明确变更范围后的工程交付，不是原完整终验通过。

## 范围决定与身份

- [A 的事前范围与冻结回执](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/56#issuecomment-6881950509)在生成数据前写入，原独立选择流程停止，不再等待 B1 或补录任何本人声明。
- 被测源码 `0292cc04ec7faa4182e97f99ccd020bf23c9ee81`，tree `1824cfc5f96fabedfac81d977b58943ddcbe4960`，执行前后工作树干净。PR #175 已交付 selector 与 runner；本次只补证据和文档，没有改产品、评分规则或执行工具。
- seed **4065886415** 是原 slot 1 / attempt 1 的 pending 候选，本次转作工程输入；**42** 是公开开发对照。工程矩阵 slot 1/2 不是原 selector 的接纳槽位，没有第二次抽取。4065886415 已暴露，不能以后补声明冒充未接触独立集。
- 原 SQLite 保留 `init → draw_intent → draw_returned` 三事件，候选仍 pending，尾锚 `4bd7241848cb734af04352f88c7a89c6440604807cec8be1868d44d13e1e969a`。原基线 [JSON](adr/0006-independent-validation-baseline.json) blob `a1e4d5346ca879f932cb31305a161d1f573e5622` 保持不变，仍 `proposed_unfrozen` / `rules_frozen=false`。工程 manifest 中的冻结字段只约束本批输入。
- 执行环境：Windows 11 `10.0.22631` AMD64，Conda pp03 / CPython 3.12.15，NumPy 2.5.4。UTC 开始 `2026-11-14T18:07:41.781314+00:00`，结束 `2026-11-14T18:08:19.800549+00:00`，即北京时间 2026-11-15。

## 固定输入与判据

[冻结规格及10份配置](evidence/engineering_20261115/freeze-spec.json)的 SHA-256 为 `bdea33418fb22d1cf560611298a869626432d5b9e71171bc50b08a4b9fb9fa97`；原始执行 manifest SHA-256 为 `0596e3d66bed62501b5c9c92b859054f8eebf3199e84e78f83d48341d89ad6be`。冻结 UTC `2026-11-14T18:07:31.621228+00:00`，早于首条执行。

配置仅改身份、种子和 normal/degradation 必需的 `scenario.kind`；三个故障配置省略 kind，保持原模板全部形状与参数。每例 65536 点、100 Hz、chunk=10240、W=256、256 窗、N=1，原黄70/红40健康规则未改。正常须零告警且召回率 null；退化须后128窗健康分中位数严格小于前128窗；六个故障必须逐一检出。故障不另设零误报门槛，仍完整报告 FP、冗余、未匹配。一对一最大匹配、右开事件区间加两窗宽限保持原算法。

精确工作树字节摘要：runner `26374c26859009232af0df774d0eae2cb565a7e527c8cf5405ded5efa7461593`；consumer `aebb18a9191f154c16e1320a73d7593774879d44573d5c10811fea8e35c08bac`；health_rules `c26bad78809424599a48ccebd7f92b1b290e9d598a0367778127c9e1b9c77f1f`。consumer/rules 是 Windows CRLF 检出字节；Git LF 与 CRLF 的差异不能当作评分改动，也不能绕过 runner 的精确哈希检查。

## 全部场景与事件结果

每例真实执行 `validate → simulate → consume → report`，一次运行，无结果导向重选或重试。10例均完整消费和分析65536点/256窗，丢弃、拒收、排除窗口均0；JSON与Markdown报告10/10齐全，无中止或未启动场景。

| seed | 场景 | 告警 | FP | 冗余 | 未匹配 | TP / FN | 判定 |
|---|---|---|---|---|---|---|---|
| 4065886415 | normal | 0 | 0 | 0 | 0 | 0 / 0 | 通过 |
| 4065886415 | degradation | 1 | 1 | 0 | 1 | 0 / 0 | 通过 |
| 4065886415 | spike | 1 | 0 | 0 | 0 | 1 / 0 | 通过 |
| 4065886415 | step | 1 | 0 | 0 | 0 | 1 / 0 | 通过 |
| 4065886415 | burst | 1 | 0 | 0 | 0 | 1 / 0 | 通过 |
| 42 | normal | 0 | 0 | 0 | 0 | 0 / 0 | 通过 |
| 42 | degradation | 1 | 1 | 0 | 1 | 0 / 0 | 通过 |
| 42 | spike | 1 | 0 | 0 | 0 | 1 / 0 | 通过 |
| 42 | step | 1 | 0 | 0 | 0 | 1 / 0 | 通过 |
| 42 | burst | 1 | 0 | 0 | 0 | 1 / 0 | 通过 |

- seed 4065886415：前半中位数 83.34144483，后半 50.71910731。
- seed 42：前半中位数 83.37154689，后半 50.93358085。

| seed | 类型 | 事件区间 | 允许区间 | 匹配告警起点 | 时延（样本） |
|---|---|---|---|---|---|
| 4065886415 | spike | [16384, 16640) | [16384, 17152) | 16384 | -256 |
| 4065886415 | step | [16384, 18432) | [16384, 18944) | 16384 | -2048 |
| 4065886415 | burst | [16384, 18432) | [16384, 18944) | 16640 | -1792 |
| 42 | spike | [16384, 16640) | [16384, 17152) | 16384 | -256 |
| 42 | step | [16384, 18432) | [16384, 18944) | 16384 | -2048 |
| 42 | burst | [16384, 18432) | [16384, 18944) | 16640 | -1792 |

退化各有1条告警，在报告中计 FP=1 / unmatched=1，未隐去，也不套用正常零告警门槛。正常召回率为 null；负时延表示匹配告警窗口起点早于事件结束，并非报告生成耗时。

## 证据与复现

- [路径脱敏的完整结果](evidence/engineering_20261115/result-redacted.json)保留全部报告、判据与40条命令退出码，仅命令中的个人绝对路径替换为占位符。原结果原始字节 SHA-256：`f04fe0056805525c2d1bc6a295bf632f6d7645b033d4b21c3aecf26384bba1d5`。
- [脱敏 manifest 视图](evidence/engineering_20261115/manifest-redacted.json)明确不是原始可执行 manifest；不要把其摘要冒充原冻结摘要。
- `evidence/engineering_20261115/reports/` 保存10例原样 JSON/Markdown报告；[公开文件哈希](evidence/engineering_20261115/public-file-hashes.json)可逐文件核对；[原始命令日志与结果哈希](evidence/engineering_20261115/original-artifact-hashes.json)索引本地完整留档。
- 原运行目录、NPZ、SQLite、原始日志和带机器路径的 manifest 仅保留在独立业务临时目录；不移入 Obsidian 或其 output，也不提交大数据或 private_truth。公开证据只含必要可读报告、配置、摘要和哈希。

复现时从上述源码 SHA 建立干净检出，使用公开配置的精确字节，按 [runner 使用说明](holdout_execution.md)建立新工程 manifest，绑定自己的源码绝对位置与字节哈希、真实授权回执和新冻结时间，再使用源码外的新目录执行。公开脱敏视图中的 `<SOURCE>/<FROZEN>/<OUTPUT>/<PYTHON>` 是说明性占位符。任何新环境或换行转换必须重新声明为新工程执行，不得改写本批原始哈希、时间或结果。

## 质量与收尾边界

被测源码主线 [CI 806](https://github.com/Zhu-ShuH/g15-predictive-maintenance/actions/runs/43623695752)四项成功；Python 3.11/3.12 各2048 passed、15 skipped。当前文档PR的CI在PR检查中单独记录，不能用旧CI替代新提交检查。

#56/#9 原独立目标按此次范围决定停止，关闭理由为 not_planned；工程工具、实际验证和报告完成另行记录。没有伪造 B1 Review 或 B2/C1/A 本人声明，没有把内部复核计为独立票。原独立验收、最终候选在双目标 Linux 的整包复证和现场验收仍未完成；未来若恢复，应另立受控范围，不能续用已暴露候选。

Gate 0 已执行 FAILED / AND=false，原9/新26失败及限制保留，100k未验证；不恢复容量修复/重测。自动提醒保持关闭，未发送真实通知、未操作真实设备。课程评分、成员个人贡献与现场答辩不在本次工程通过结论内。
