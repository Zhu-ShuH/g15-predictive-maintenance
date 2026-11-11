# 成员贡献与可核对证据

> 负责人 A；2026-10-28 初版；2026-10-30 状态同步；2026-11-10 按
> `main@997acaebef2d4ab72639954afe8078bd9a04452b` 增补冻结候选核验。
> 下文 §2–§5 是 `b3daf15e` 时点的历史实物索引，原文保留用于追溯，不应再被解读为
> 当前 PR 状态。本表不是工时评分或本人签字表；当前计数必须与实际 Release SHA 一起重算。

## 1. 登记规则

以 Issue 范围、实际文件、PR 作者、提交署名、正式 Review 与成员确认共同判断归属；不按 commit 数、代码行数或测试通过数量评定贡献。PR 作者和名义 CODEOWNER 不自动等于全部实际作者，合法代改与共同署名按原 Git 记录保留。身份核对见 [contributors](contributors.md)，职责入口见 [.github/CODEOWNERS](../.github/CODEOWNERS)。

“已合入”须有 merged_at 和合并提交；“候选”只是已提交的实物；“评审/治理”是独立工作类型，不把发现缺陷算成亲自实现修复。关闭未合入的替代 PR、同一修复的多轮提交和重复跑测试不重复算作多份交付。A 的内部复核不计成多名独立评审，也不代替成员人工批准。

### 1.1 冻结候选的 commit 与周频次核验

教师要求“姓名 × 角色 × 子系统 × commit 数”，并现场用 `git shortlog` 核对。
下面是对 `997acaebef2d4ab72639954afe8078bd9a04452b` 的本地 Git 对象执行
`git shortlog -sne HEAD` 得到的**主线 commit 对象数**，包括 merge commit；它不是
PR 数、代码行数或贡献价值。别名只在 `docs/contributors.md` 已有账号/邮箱依据时合并。

| 角色 | 已知身份归并 | main commit 数 | 子系统/主要证据类型 | 核验状态 |
|---|---|---:|---|---|
| A | `ZSH` + `Zhu-ShuH`（163 邮箱与 GitHub noreply） | 73 | 治理、契约、ADR、CI、Review、集成证据 | 有 commit；内容仍按下文 Issue/PR 逐项核 |
| B1 | `kittywent` | 15 | 仿真内核、性能、承接报告/验证、Review | 有 commit |
| B2 | `ryj-teach1`（163/QQ 与 GitHub noreply） | 7 | 特征、健康分、告警、数值边界 | 有 commit |
| C1 | `wzs-1234` | 2 | spike/step/burst 与真值 | 有 commit；周频次不足 |
| C2 | `zty-78`（两个已出现邮箱） | 14 | 消费、SQLite、恢复、只读路径 | 有 commit |
| C3 | `Www9191` | 8 | YAML、配置校验、薄 CLI | 有 commit |
| C4 | `wxm-1` | 10 | Schema、NPZ/Manifest、生产入口 | 有 commit |
| C5 | `Gyy-yyy` | 0 | 原独立验证/报告角色 | **未满足“人人有 commit”**；B1 承接不能改署 |
| C6 | `Ran2149975600` + `La Palome`（同一邮箱） | 8* | 脚本、双 Linux、运维证据 | C6 本人已确认：显示名与登录名为同一 GitHub 账号，同邮箱 8 条提交均归属 C6 |

`*` 同邮箱是核查线索，不等于本人签字。最终表需要成员确认账号/邮箱和具体提交，
不得仅凭邮箱相同强行归属。

按 ISO 周从主线提交日期统计，别名采用上表的暂定归并，得到：

| 角色 | W42 | W43 | W44 | W45 | W46（截至 11-10） | “每周 ≥3”字面核查 |
|---|---:|---:|---:|---:|---:|---|
| A | 8 | 15 | 11 | 33 | 6 | 当前记录满足 |
| B1 | 1 | 4 | 3 | 5 | 2 | **不满足**（W42/W46） |
| B2 | 0 | 1 | 5 | 1 | 0 | **不满足** |
| C1 | 0 | 1 | 1 | 0 | 0 | **不满足** |
| C2 | 0 | 1 | 1 | 3 | 9 | **不满足** |
| C3 | 1 | 1 | 1 | 3 | 2 | **不满足** |
| C4 | 0 | 5 | 0 | 4 | 1 | **不满足** |
| C5 | 0 | 0 | 0 | 0 | 0 | **不满足** |
| C6 | 1 | 1 | 0 | 6 | 0 | **不满足**；别名已由 C6 本人确认 |

W42 是项目起始周、W46 是截至 11-10 的未结束周；是否按完整周评分由教师裁定，
本表不擅自把部分周改判为通过。历史频次不能用空 commit、拆分同一改动或改写 main
补造；仍在开发期的成员只能通过后续真实、有意义、经 Review 的交付改善未来周记录。

现场复现命令：

```bash
git rev-parse HEAD
git shortlog -sne HEAD
git log HEAD --format='%ad%x09%an%x09%ae' --date=format:'%G-W%V' \
  | sort | uniq -c
```

PowerShell 可用：

```powershell
git log HEAD --format='%ad`t%an`t%ae' --date=format:'%G-W%V' |
  Sort-Object | Group-Object |
  ForEach-Object { "{0}`t{1}" -f $_.Count, $_.Name }
```

Release 冻结时把 `HEAD` 换成完整候选 SHA，并同时核对：

1. 每名成员至少一个有意义的 feature/fix/test/docs PR，文件与角色输入输出能讲清；
2. 每个 PR 至少一名非作者真实 Review，旧 head 的批准不能沿用到新 head；
3. squash/co-author/代改保留原作者，不把 A 的合并提交或 B1 承接工作重复分给他人；
4. commit 数与 Issue/PR/Review/测试证据能相互解释，数量差异有书面理由。

当前硬缺口是 C5 为 0；本文件只能披露，不能替成员生成提交。若实际答辩队伍已变化，
由 A 按真实名单和教师规则更新组别/职责，不保留虚构九人满编口径。

## 2. 九角色交付索引

| 角色 / GitHub | 可核对的实际交付 | 状态及边界 |
|---|---|---|
| A / [Zhu-ShuH](https://github.com/Zhu-ShuH) | 仓库与CI [#1](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/1)；规划 [#18](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/18)、验收/环境 [#21](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/21)、指南 [#14](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/14)、CODEOWNERS [#19](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/19)；共享类型 [#31](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/31)；文档引用检查 [#25](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/25)；身份检查 [#41](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/41)/[#49](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/49)；契约说明 #37/#50/#60；候选ADR [#61](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/61)/[#62](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/62) | 列举PR均已合入；ADR-0005/0006仍为未冻结提案。A接口/实测评审与归档另列，不把模块作者贡献归给A |
| B1 / [kittywent](https://github.com/kittywent) | 仿真内核 [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15)、黄金值/ADR [#33](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/33)、时间戳与采样率 [#42](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/42)/[#47](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/47)、完整生产性能记录 [#53](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/53)；转派后的报告/校验候选 #70/#72；多项独立评审见下表 | 前五项已合入；#53是Windows证据，非双Linux验收；转派候选#70/#72未合入，按实际作者B1登记 |
| B2 / [ryj-teach1](https://github.com/ryj-teach1) | 特征、健康评分、告警及规则 [#54](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/54)；数值边界修复候选 #73；[#72 R5正式评审](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5677015028) | #54已合入；#73已提交e4814b65769fa6da45ca070daac89af02d03d0e9整改候选（含大端字节序修复），A已在该head复审APPROVED5698934386，其他成员审查/集成未齐；R5由B2发现与复现，并已本人[复审APPROVED5684239091](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5684239091)覆盖其旧CR；这是评审贡献，修复实现属于B1，A另有独立复证；另在#70提出场景类型判定风险，A复证并请求修改，整改实现仍归B1 |
| C1 / [wzs-1234](https://github.com/wzs-1234) | spike/step/burst可控注入与真值 [#27](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/27) | 已合入；波形用例不自动等于三类可靠性故障注入或正式检出率验收 |
| C2 / [zty-78](https://github.com/zty-78) | 消费入口、SQLite、窗口/告警状态与恢复 [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64)；末片上界修复 #76；[#57本人契约答复](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57#issuecomment-6358549345) | #64已合入；#76已合入；公开接口确认、真实Linux强杀证据分别跟踪，不用单元恢复结果代替 |
| C3 / [Www9191](https://github.com/Www9191) | 配置/YAML/validate [#26](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/26)、配置采样率上限 [#55](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/55)；消费 CLI 接线 [#71](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/71)；benchmark [#92](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/92) | #26/#55/#71/#92 均已合入；薄 CLI 职责不等于实现上游仿真或下游业务公式 |
| C4 / [wxm-1](https://github.com/wxm-1) | Schema/NPZ读写 [#29](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/29)、生产入口 [#30](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/30)、契约收口 [#43](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/43)、真值写入失败语义 [#48](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/48)、末片含等号勘误 [#68](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/68) | 均已合入；#68是契约修订，#76是C2的消费端实现，分别登记 |
| C5 / [Gyy-yyy](https://github.com/Gyy-yyy) | 名义主责为独立验证/报告/测试汇总；本快照未登记到可核验的C5本人独立PR交付条目 | 待本人补具体链接/文件/结果；不根据名义角色把B1的#70/#72归给C5，也不据本表缺记录推断其全部工作情况；A已于2026-10-25将#9/#51原C5相关任务转派B1（见两Issue转派评论），当前交付不再等待C5 |
| C6 / [Ran2149975600](https://github.com/Ran2149975600) | 部署脚本与双 Linux 部署/运维文档 [#86](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/86)、benchmark 状态同步与双机基准 [#97](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/97)；执行位修复 [#59](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/59) | #86/#97/#59 均已合入；依赖锁定与双解释器由 #28 关闭，双 Linux 五步 G4 与 SIGKILL 由 #10 关闭；不再写“继续待交付” |

## 3. A 的评审、治理与证据交付

以下单列评审成果，避免只统计代码PR而遗漏可复核的缺陷发现和协作工作；也避免把实现者的修复重复记为A编写。

| 对象 | A实际交付 / 固定证据 | 对应实现与边界 |
|---|---|---|
| 消费集成 #64 | [复审5637899740（COMMENTED，评审评论而非批准/请求修改结论）](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64#pullrequestreview-5637899740)；固定8b47fe307ffeacffd7381e917afa1daed53b6e2b的公开入口/SQLite及测试复核 | C2实现；后合入c4f1cfa3，A不是消费代码主作者 |
| 报告/验证 #70 | [复审5650335018](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70#pullrequestreview-5650335018)；固定144f7ae20f5be73f3bde37026fe6118087aae8d8的整改复核 | B1实现，候选未合入；B2新提出场景类型推断问题，A独立复证并[请求修改5685423787](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70#pullrequestreview-5685423787)，覆盖A历史批准；A复核不替成员合并审批 |
| 原始校验 #72 | [首次CR5668814813](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5668814813)发现等长漏检/非法seq异常/NPY容器错误；[R4 CR5669551972](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5669551972)发现零维数组中断；[R5复审5680893031](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5680893031)核收91bfb76c5490c8e9956d4dc59e78cad6b2fc625f | R5由B2先正式提出，A独立复现并定汇总口径；B1实施修复。A本轮577 passed/9 skipped是该候选Windows结果，不是main或双Linux成绩 |
| 数值边界 #73/#82 | [CR5677021053](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5677021053) 与 A 对 1.1/1.2 的独立复核、B2 实现 | #73 已合入 `0a637fc2`，A/B1/C4/C2 均批准 `e4814b65`；#82 已由 #91 合入 `51cd99ad`，1.0/1.1 时期部分掩码与全掩码 float 丢弃、全掩码整数泄漏 MaskError 的历史行为，已在 1.2 改为掩码非空即 `ValueError` 拒收，空掩码继续接受；“发现”与“完成修复”分开 |
| 末片上界 #76 | [复审5678464233](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/76#pullrequestreview-5678464233)；固定dfd5fd7984831f88f394f978be84db5d1117e98f公开consume_run与空表拒收边界实测 | C2实现；A给出同任务测试落点补充授权及接口文稿方向，不代C4确认 |
| 接口正文与归档 | [#67](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/67)具体接口文稿；[#75](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75)、[#78](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78)分版本脱敏日志、失败、环境及原始/发布摘要索引 | #67仍为候选；#75/#78已经B1/C4审阅合入，归档不是新跑测试，候选文档不是接口已冻结，历史证据不按通过数量重复计贡献 |

## 4. 交付与候选固定引用及人工核对

| PR | 本次核对的完整head | 实际作者 / 尚待完成 |
|---|---|---|
| #67 | 946ee7fa536f828243e3f8a5253da97a72a6bfb6 | A；上一head57f52d7f获B1批准5700636876/C4批准5701214981；当前head为#76合后现状同步增量，已另送审，公共接口与兼容前置保留 |
| #70 | 144f7ae20f5be73f3bde37026fe6118087aae8d8 | B1；场景类型判据新增A请求修改，等本人方案/整改；集成/验收仍保留 |
| #71 | 6c1461094585bc63fee9a25ab3cd1f3b33fa9e14 | C3；受影响角色确认 |
| #72 | 91bfb76c5490c8e9956d4dc59e78cad6b2fc625f | B1；B2已本人复审APPROVED5684239091，C4 COMMENTED5701198624三块通过，正文范围待补/C3与接口条件保留 |
| #73 | e4814b65769fa6da45ca070daac89af02d03d0e9 | B2；A已在当前head复审APPROVED，B1旧CR5679870416已由本人在当前head复审[5700184005](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5700184005)覆盖，C4已于[5701409871](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5701409871)复审APPROVED，C2等门禁保留 |
| #75 | 6b61617ff1bd75d86052e945f2d1d5945609625d | A；B1/C4已批准；#75合入`5e98a68aa857241d36211a288d6fc804b8331ffb` |
| #76 | dfd5fd7984831f88f394f978be84db5d1117e98f | C2；A/B1/C4已批准并合入`b3daf15ee8d53d7515f32b13b57de149f6767561`，不代真实Linux/接口冻结 |
| #78 | 83b01bd59a3f768b39ba0590df72ea416f4079a6 | A；B1/C4已批准；#78合入`93fcf287c90750dc8331bee55245ab63bfc1d156` |

B1 已对 #75 原head6e96588f的归档作[独立批准5679923734](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75#pullrequestreview-5679923734)，对#78原head405cae53作[独立批准5679927916](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78#pullrequestreview-5679927916)，对#76固定dfd5fd79作[独立批准5679886714](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/76#pullrequestreview-5679886714)。这些是B1的独立评审贡献；旧head批准现已因README新提交标为DISMISSED，不能写成仍对当前head有效。B1已在#75当前6b61617f重新[批准5683037430](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75#pullrequestreview-5683037430)，在#78当前83b01bd5重新[批准5683039485](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78#pullrequestreview-5683039485)；新旧Review分别留证，不重复计为两份独立审批。B1对#73的[独立CR5679870416](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5679870416)也属于可核对评审实物，不重复登记为一次新实现；该CR已由B1本人在当前head用[5700184005](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5700184005)覆盖。

## 5. 合并证据与维护方式

主要合并锚点：#31=`e8e29248eb7355ebaf1a31b2de7c41f85830dbf4`；#54=`81c13d17faf8189a240989b4ee43b937820bb911`；#64=`c4f1cfa3ef6fb9f670fce82ceeb6abafeafdc770`；#61=`6004da53c9d0304faf89b9c05fc41f47afaef010`；#62=`85bc6a32b2be0364c2057883e95f0efbe66576b1`；#53=`75b119454a1ac075620bdcc74826730fa7b4ac02`。PR页面记录详细文件、作者、Review与合并时间；本表不改写Git历史。

本初版由A按公开实物整理，尚不代表九人分别确认全部条目。成员补充时提交具体Issue/PR/文件/目标SHA/Review或报告链接；发现署名与实际责任不符时保留原始证据并说明更正，不按账号展示名猜测。未有证据的工时、个人声明、Linux运行、独立验证和最终验收保持未知。后续按实物更新同一表，不为增加提交次数拆分重复记录。
