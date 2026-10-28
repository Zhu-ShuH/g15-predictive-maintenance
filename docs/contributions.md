# 成员贡献与可核对证据

> 负责人 A；2026-10-28 初版，按同日实际评审增补。基准 main `75b119454a1ac075620bdcc74826730fa7b4ac02`，候选按下列固定 SHA 记录。本表是交付证据索引，供成员核对，不是工时统计、评分或本人签字表。

## 1. 登记规则

以 Issue 范围、实际文件、PR 作者、提交署名、正式 Review 与成员确认共同判断归属；不按 commit 数、代码行数或测试通过数量评定贡献。PR 作者和名义 CODEOWNER 不自动等于全部实际作者，合法代改与共同署名按原 Git 记录保留。身份核对见 [contributors](contributors.md)，职责入口见 [.github/CODEOWNERS](../.github/CODEOWNERS)。

“已合入”须有 merged_at 和合并提交；“候选”只是已提交的实物；“评审/治理”是独立工作类型，不把发现缺陷算成亲自实现修复。关闭未合入的替代 PR、同一修复的多轮提交和重复跑测试不重复算作多份交付。A 的内部复核不计成多名独立评审，也不代替成员人工批准。

## 2. 九角色交付索引

| 角色 / GitHub | 可核对的实际交付 | 状态及边界 |
|---|---|---|
| A / [Zhu-ShuH](https://github.com/Zhu-ShuH) | 仓库与CI [#1](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/1)；规划 [#18](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/18)、验收/环境 [#21](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/21)、指南 [#14](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/14)、CODEOWNERS [#19](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/19)；共享类型 [#31](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/31)；文档引用检查 [#25](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/25)；身份检查 [#41](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/41)/[#49](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/49)；契约说明 #37/#50/#60；候选ADR [#61](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/61)/[#62](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/62) | 列举PR均已合入；ADR-0005/0006仍为未冻结提案。A接口/实测评审与归档另列，不把模块作者贡献归给A |
| B1 / [kittywent](https://github.com/kittywent) | 仿真内核 [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15)、黄金值/ADR [#33](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/33)、时间戳与采样率 [#42](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/42)/[#47](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/47)、完整生产性能记录 [#53](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/53)；转派后的报告/校验候选 #70/#72；多项独立评审见下表 | 前五项已合入；#53是Windows证据，非双Linux验收；转派候选#70/#72未合入，按实际作者B1登记 |
| B2 / [ryj-teach1](https://github.com/ryj-teach1) | 特征、健康评分、告警及规则 [#54](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/54)；数值边界修复候选 #73；[#72 R5正式评审](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5677015028) | #54已合入；#73已提交5314f905整改候选并获A复审，其他成员审查/集成未齐；R5由B2发现与复现，并已本人[复审APPROVED5684239091](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5684239091)覆盖其旧CR；这是评审贡献，修复实现属于B1，A另有独立复证；另在#70提出场景类型判定风险，A复证并请求修改，整改实现仍归B1 |
| C1 / [wzs-1234](https://github.com/wzs-1234) | spike/step/burst可控注入与真值 [#27](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/27) | 已合入；波形用例不自动等于三类可靠性故障注入或正式检出率验收 |
| C2 / [zty-78](https://github.com/zty-78) | 消费入口、SQLite、窗口/告警状态与恢复 [#64](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64)；末片上界候选 #76；[#57本人契约答复](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/57#issuecomment-6358549345) | #64已合入；#76未合入；公开接口确认、真实Linux强杀证据分别跟踪，不用单元恢复结果代替 |
| C3 / [Www9191](https://github.com/Www9191) | 配置/YAML/validate [#26](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/26)、配置采样率上限 [#55](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/55)；消费CLI候选 #71 | #26/#55已合入；#71未合入；薄CLI职责不等于实现上游仿真或下游业务公式 |
| C4 / [wxm-1](https://github.com/wxm-1) | Schema/NPZ读写 [#29](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/29)、生产入口 [#30](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/30)、契约收口 [#43](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/43)、真值写入失败语义 [#48](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/48)、末片含等号勘误 [#68](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/68) | 均已合入；#68是契约修订，#76是C2的消费端实现，分别登记 |
| C5 / [Gyy-yyy](https://github.com/Gyy-yyy) | 名义主责为独立验证/报告/测试汇总；本快照未登记到可核验的C5本人独立PR交付条目 | 待本人补具体链接/文件/结果；不根据名义角色把B1的#70/#72归给C5，也不据本表缺记录推断其全部工作情况；A已于2026-10-25将#9/#51原C5相关任务转派B1（见两Issue转派评论），当前交付不再等待C5 |
| C6 / [Ran2149975600](https://github.com/Ran2149975600) | shell脚本可执行位修复 [#59](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/59) | 已合入；仅证明执行位修正，脚本实现、部署/运维、依赖锁定、双Linux与真实强杀继续待交付 |

## 3. A 的评审、治理与证据交付

以下单列评审成果，避免只统计代码PR而遗漏可复核的缺陷发现和协作工作；也避免把实现者的修复重复记为A编写。

| 对象 | A实际交付 / 固定证据 | 对应实现与边界 |
|---|---|---|
| 消费集成 #64 | [复审5637899740（COMMENTED，评审评论而非批准/请求修改结论）](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/64#pullrequestreview-5637899740)；固定8b47fe307ffeacffd7381e917afa1daed53b6e2b的公开入口/SQLite及测试复核 | C2实现；后合入c4f1cfa3，A不是消费代码主作者 |
| 报告/验证 #70 | [复审5650335018](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70#pullrequestreview-5650335018)；固定144f7ae20f5be73f3bde37026fe6118087aae8d8的整改复核 | B1实现，候选未合入；B2新提出场景类型推断问题，A独立复证并[请求修改5685423787](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/70#pullrequestreview-5685423787)，覆盖A历史批准；A复核不替成员合并审批 |
| 原始校验 #72 | [首次CR5668814813](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5668814813)发现等长漏检/非法seq异常/NPY容器错误；[R4 CR5669551972](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5669551972)发现零维数组中断；[R5复审5680893031](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/72#pullrequestreview-5680893031)核收91bfb76c5490c8e9956d4dc59e78cad6b2fc625f | R5由B2先正式提出，A独立复现并定汇总口径；B1实施修复。A本轮577 passed/9 skipped是该候选Windows结果，不是main或双Linux成绩 |
| 数值边界 #73 | [CR5677021053](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5677021053)；固定ca83aec31d64d1ec8dd7c4fad7f000ee56e4b713的dtype/整数精度及兼容性实证 | B2已提交5314f905整改，A已[复审APPROVED5685422639](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5685422639)；仍待其他成员审查及集成；“发现”与“完成修复”分开 |
| 末片上界 #76 | [复审5678464233](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/76#pullrequestreview-5678464233)；固定dfd5fd7984831f88f394f978be84db5d1117e98f公开consume_run与空表拒收边界实测 | C2实现；A给出同任务测试落点补充授权及接口文稿方向，不代C4确认 |
| 接口正文与归档 | [#67](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/67)具体接口文稿；[#75](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75)、[#78](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78)分版本脱敏日志、失败、环境及原始/发布摘要索引 | 均为已提交候选；归档不是新跑测试，候选文档不是接口已冻结，历史证据不按通过数量重复计贡献 |

## 4. 候选固定引用与人工核对

| PR | 本次核对的完整head | 实际作者 / 尚待完成 |
|---|---|---|
| #67 | 6871657c3d4b8a007f74b5b912c00e91c2164289 | A；公共接口审查与兼容前置 |
| #70 | 144f7ae20f5be73f3bde37026fe6118087aae8d8 | B1；场景类型判据新增A请求修改，等本人方案/整改；集成/验收仍保留 |
| #71 | 6c1461094585bc63fee9a25ab3cd1f3b33fa9e14 | C3；受影响角色确认 |
| #72 | 91bfb76c5490c8e9956d4dc59e78cad6b2fc625f | B1；B2已本人复审APPROVED5684239091，相关接口条件保留 |
| #73 | 5314f905e92adb3cfcdd03dde6d20463b138cc30 | B2；整改已获A复审通过，B1旧CR须本人复审，C4/C2等门禁保留 |
| #75 | 6b61617ff1bd75d86052e945f2d1d5945609625d | A；B1已复审当前README增量，仍等C4 |
| #76 | dfd5fd7984831f88f394f978be84db5d1117e98f | C2；相关成员审查与集成 |
| #78 | 83b01bd59a3f768b39ba0590df72ea416f4079a6 | A；B1已复审当前README增量，仍等C4 |

B1 已对 #75 原head6e96588f的归档作[独立批准5679923734](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75#pullrequestreview-5679923734)，对#78原head405cae53作[独立批准5679927916](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78#pullrequestreview-5679927916)，对#76固定dfd5fd79作[独立批准5679886714](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/76#pullrequestreview-5679886714)。这些是B1的独立评审贡献；旧head批准现已因README新提交标为DISMISSED，不能写成仍对当前head有效。B1已在#75当前6b61617f重新[批准5683037430](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75#pullrequestreview-5683037430)，在#78当前83b01bd5重新[批准5683039485](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/78#pullrequestreview-5683039485)；新旧Review分别留证，不重复计为两份独立审批。B1对#73的[独立CR5679870416](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/73#pullrequestreview-5679870416)也属于可核对评审实物，不重复登记为一次新实现。

## 5. 合并证据与维护方式

主要合并锚点：#31=`e8e29248eb7355ebaf1a31b2de7c41f85830dbf4`；#54=`81c13d17faf8189a240989b4ee43b937820bb911`；#64=`c4f1cfa3ef6fb9f670fce82ceeb6abafeafdc770`；#61=`6004da53c9d0304faf89b9c05fc41f47afaef010`；#62=`85bc6a32b2be0364c2057883e95f0efbe66576b1`；#53=`75b119454a1ac075620bdcc74826730fa7b4ac02`。PR页面记录详细文件、作者、Review与合并时间；本表不改写Git历史。

本初版由A按公开实物整理，尚不代表九人分别确认全部条目。成员补充时提交具体Issue/PR/文件/目标SHA/Review或报告链接；发现署名与实际责任不符时保留原始证据并说明更正，不按账号展示名猜测。未有证据的工时、个人声明、Linux运行、独立验证和最终验收保持未知。后续按实物更新同一表，不为增加提交次数拆分重复记录。
