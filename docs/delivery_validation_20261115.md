# #177 交付候选实测记录

## 版本与证据范围

本轮产品源码固定为 `16d57df485d428c02914bac372b61c67917857da`，tree为 `ff341152a41daa68cbc3d08597649c386f3332ca`。Ubuntu与RHEL各自从公开仓库新克隆，在独立Conda `pp03` 中安装锁定依赖；正式验收使用干净源码的Git归档，所有运行和测试目录均在规划库外。原克隆保持干净，受测源码480文件的前后摘要相同。

后续提交只补文档与可读证据，受测版本不会追写成未执行的新SHA。PR最终文档head的CI与正式Review另行登记；任何后续产品、配置、脚本或依赖变化都不能直接沿用本轮成绩。[汇总JSON](evidence/delivery_20261115/linux/summary.json)、[质量记录](evidence/delivery_20261115/candidate-quality.json)和各平台清单区分原始文件与脱敏副本摘要。

## 双系统与质量

| 项目 | Ubuntu | RHEL |
|---|---|---|
| 系统 | Ubuntu24.04.5 LTS，x86_64 | RHEL9.7，x86_64 |
| CPU/内存 | AMD Ryzen7 7435H，16个逻辑CPU；MemTotal 16,323,992kB | AMD Ryzen7 7435H，4个逻辑CPU；MemTotal 16,077,068kB |
| 环境 | Conda26.9.4，pp03 Python3.12.16 | Conda26.10.0，pp03 Python3.12.16 |
| 依赖 | NumPy2.5.4、PyYAML6.0.3；21项适用锁版本匹配，pip check成功 | 同左 |
| 完整pytest | 2075 passed、15 skipped，231.41s | 2075 passed、15 skipped，213.45s |
| 质量检查 | verify范围src/tests的Black125文件；Pylint9.67/10 | 同左 |
| 故障套件 | 11 passed，3.86s | 11 passed，2.67s |
| acceptance | verify/fault/benchmark的命令和tee退出码全部0，总退出0 | 同左；完整验收在隔离外网的netns中执行 |

Black的工程tools部分另由同产品树CI检查21文件，与src/tests合计146文件。Windows提交前完整回归为2067 passed/23 skipped；Python3.11/3.12 CI各2075 passed/15 skipped。平台或显式opt-in的skip不是通过成绩，容量测试没有被自动启用。

环境、Conda/pip原始输出、存储采集时点与文件系统分别见平台清单：[Ubuntu](evidence/delivery_20261115/linux/ubuntu/evidence-manifest.json)、[RHEL](evidence/delivery_20261115/linux/rhel/evidence-manifest.json)。存储类型按实际文件系统记录，未测SSD/HDD介质属性。

## 百万点三轮

每轮为独立空运行和新CLI进程，固定入库的 `benchmark_1m.yaml`、seed42、1,000,000点、10分片、2,000,000信号标量。三个时间均为未舍入的完整生产窗口，包含生成、序列化、SHA及complete发布。三轮全部保留，校验通过，CLI/tee均0。

| 系统 | 第1轮/s | 第2轮/s | 第3轮/s | 中位数/s | 最大值/s | 严格<60s |
|---|---:|---:|---:|---:|---:|---|
| Ubuntu | 0.30987831999664195 | 0.27854423799726646 | 0.20871937499759952 | 0.27854423799726646 | 0.30987831999664195 | 三轮均通过 |
| RHEL | 0.2411544540009345 | 0.23643303800054127 | 0.22548573200037936 | 0.23643303800054127 | 0.2411544540009345 | 三轮均通过 |

原始逐轮记录在各平台的 `benchmark/run1.log`–`run3.log` 与 `benchmark/steps.tsv`。没有只选最快一轮；不宣称清空OS缓存或专用物理资源。**本轮基准CLI总墙钟和基准进程峰值RSS未测**，不将生产时间或整包用时冒充这两项数字。

## 禁网、恢复与配置对比

| 验证 | Ubuntu | RHEL |
|---|---|---|
| 完整acceptance的网络范围 | 正常网络，不能称整包OS禁网 | 用户/网络命名空间，仅lo、路由0；本机TCP往返成功，外连内核errno101 |
| 核心链路禁网 | 单独在内核seccomp进程树下执行；原生libc创建AF_INET/AF_INET6均errno1，NoNewPrivs=1、Seccomp=2，无继承socket | 同一netns内执行，允许本机通信，不能访问外网 |
| 配置A/B | 入库healthy/degradation seed42各执行validate→simulate→consume→report；源码和评分摘要不变 | 同样执行 |
| 坏分片 | 真实字节修改使SHA不同；拒收后feature_windows=0 | 同样通过 |
| SIGKILL与重放 | 实际returncode=-9；7片中已提交1片时强杀，重启处理其余6片；五张业务表与不中断基线逐内容相同 | 同样通过 |

Ubuntu不能创建无特权网络命名空间，本轮没有改变主机安全策略；其证明范围是核心流程及保留故障用例的进程树禁网，不是整机断网或完整HTTP测试套件禁网。RHEL完整验收允许loopback，因为查询/控制台测试需要本机HTTP。两种证据都不代表真实网络设备或现场教师注入。

配置A/B每例65536点。两系统均为healthy零告警；degradation保留1条告警，健康分前后半中位数为83.37154688718988→50.93358084850047。无真实事件时detection_rate=null，不写成100%。配置、规则和报告摘要以及实际命令见[Ubuntu A/B](evidence/delivery_20261115/linux/ubuntu/config-ab.json)、[RHEL A/B](evidence/delivery_20261115/linux/rhel/config-ab.json)。这使用既有公开配置，不是新独立集。

强杀检查点原始stdout见各平台 `retained-fault-tests.log`；五表逐内容摘要分别见[Ubuntu](evidence/delivery_20261115/linux/ubuntu/five-table-reconciliation.json)、[RHEL](evidence/delivery_20261115/linux/rhel/five-table-reconciliation.json)。表为runs、processed_chunks、feature_windows、alarms、alarm_states；不是只比较行数。

## README演示入口

两系统另在全新的外部Git归档运行副本内执行 `bash scripts/demo.sh`，起始为真实空runs目录；候选480个源码文件前后不变。Ubuntu在seccomp下退出0、整条demo墙钟13.896255709005345秒；RHEL在netns下退出0、13.195033841999248秒。两次均生成100万点的sudden_fault公开回归报告，passed/verifiable=true、10条告警。该墙钟包含demo的多阶段链路，与上一节基准生产时间不同。

实际命令、报告与隔离证明见[Ubuntu demo](evidence/delivery_20261115/linux/ubuntu/demo/demo-result.json)、[RHEL demo](evidence/delivery_20261115/linux/rhel/demo/demo-result.json)及同目录原始输出/报告。两台首次补跑使用了外部适配器的runs符号链接，`find runs`不跟随该链接而在生成后退出3；改用新的真实空目录运行副本后成功。失败及原始日志摘要保留在各平台manifest的`demo_first_attempt`，没有改候选脚本或删除失败记录。

## 准备失败与保留边界

RHEL在业务验收前曾因外部适配器PATH缺少已有的/usr/sbin退出127，再因探针读取宿主挂载的sysfs接口视图退出1。后者当时的原生外连已被内核拒绝、路由为空。修正外部适配器以读取当前命名空间的if_nameindex后，才执行成功的完整验收；候选代码和判据均未改。三次状态及原始摘要见[准备历史](evidence/delivery_20261115/linux/rhel/preparation-history.json)。独立环境初次离线克隆缺包的准备失败也保留于本地原始记录，未改历史环境。

公开证据只保留环境、命令、退出码、摘要、日志和报告；主机名、私网地址、个人路径及绝对临时目录已脱敏。原始文件与公开副本的SHA256分别列出；NPZ、SQLite、pytest目录和凭据不进入公开交付包。扫描与A自核不代替PR最终head的真实非作者Review。

本轮补齐的是候选工程运行证据：原独立验收未满足、Gate0 FAILED/AND=false、100k未验证的状态不变。成员本人贡献/讲解和教师范围认定继续按[人工清单](delivery_readiness_20261115.md)处理，不由本报告代填。
