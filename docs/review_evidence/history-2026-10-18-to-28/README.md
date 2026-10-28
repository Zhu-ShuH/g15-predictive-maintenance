# A 历史测试与诊断证据（2026-10-18 至 2026-10-28）

本归档保存 A 既有本地执行记录，本次整理上传没有重跑测试。每组 metadata.json 说明提交绑定、已记录环境、结果与限制。原始失败、重跑、预期拒绝、跳过、格式检查失败均保留。未记录的命令、环境和退出码不补造；通过数量不相加作为验收成绩。

较近六组证据已另存于 [PR #75](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/75)，本目录补充更早记录及 10 月 28 日 PR #76 记录，不重复复制这六组。

完整 SHA 也有不同证明强度：部分由诊断自身记录，部分仅来自同次远端候选快照。上下文绑定不等于逐文件源码证明。本地原型/环境检查未恢复精确提交，不计作当前 main 或远端候选验收。混合来源诊断以子记录自身来源为准。

| 记录组 | 候选或上下文 SHA | 绑定状态 |
|---|---|---|
| 20261018-pr26 | a84ca883d7110fd63c2eddbd2debad49515d7977 | recorded candidate context |
| 20261018-pr27 | 未绑定 | commit binding unavailable |
| 20261018-main | 8d29cd3b689a2068eee38ff9920cf577368904ee | recorded candidate context |
| 20261018-early-probes | 未绑定 | commit binding unavailable |
| 20261019-pr30 | 2c20de33d0cf0d56ae45559db4ad008d3a585a10 | recorded candidate context |
| 20261019-pr33 | f872ce42e624138b7f6cf32cd96eeb5bc4306ded | recorded candidate context |
| 20261019-pr43 | 8042e5194d60d56866d1dd351de58b2842908daa | recorded candidate context |
| 20261019-pr42 | 39a90758770c5a9f77fac3dc11b07af8fe714b2a | recorded candidate context |
| 20261020-pr43 | b2feacfb312f77af4d8f7fb33c9512032ee72608 | recorded candidate context |
| 20261020-pr47 | 48936ecc420aa9ad6ccdedc0d6abea8c4a5427e1 | recorded candidate context |
| 20261021-pr55 | b80caa6b6107a75c2b2ba1d5e1e224f2baf98abe | recorded candidate context |
| 20261021-main-probe | 4c4e025eee09cfe8c787adb40db6e63895e386cb | recorded candidate context |
| 20261022-pr54-initial | 490a42ed8249970c4aa3e6378cbffddb3d34fc82 | recorded candidate context |
| 20261022-pr54-later | da3a52b9105edfd0c418c7c29369fd6c68aab01d | recorded candidate context |
| 20261023-pr64 | e1593ff707925cf4ebee2758a332be6996783d25 | recorded candidate context |
| 20261023-comparison-main | 81c13d17faf8189a240989b4ee43b937820bb911 | recorded candidate context |
| 20261023-comparison-pr64 | e1593ff707925cf4ebee2758a332be6996783d25 | recorded candidate context |
| 20261023-local-baseline | 2c1475938e2f90d9741d055f2b47e71174583bd2 | local HEAD context, working-tree state not attested |
| 20261023-local-prototype | 未绑定 | commit binding unavailable |
| 20261024-pr64 | 8b47fe307ffeacffd7381e917afa1daed53b6e2b | recorded candidate context |
| 20261024-producer | 82bafa437e6b35f56f9c034ce1cc5402ceb3e7a2 | recorded candidate context |
| 20261026-pr70-initial | d6fe35112ca91207d7ca9c1f82f0746885264604 | recorded candidate context |
| 20261026-pr70-second | 80b95d8c726daa6c95ac26aca5a64e1724399433 | recorded candidate context |
| 20261026-pr70-third | 0fbcc87f0d336af9ab1f02e35343f79e8d0e76d8 | recorded candidate context |
| 20261026-local-environment | 未绑定 | commit binding unavailable |
| 20261028-pr76 | dfd5fd7984831f88f394f978be84db5d1117e98f | recorded candidate context |

## 发布转换与范围

artifact-index.json 为每份原始证据保存原始字节 SHA-256、发布字节 SHA-256、编码和转换状态。日志保留结果及相对源码路径，个人绝对路径替换为 <LOCAL_PATH>/<SOURCE_ROOT>，本地用户名与内部工具标识替换为稳定标记/A。JSON 重新排版并处理嵌套 JSON 字符串；旧日志原有编码损伤不修造。摘要索引用于对照本地原件，不代表上传了原始私有字节。

未上传私有规划笔记、API 审计快照、源码副本、候选测试源码、虚拟环境、运行临时目录、凭据、包清单、ACL 或完整环境导出。已在远端存在的下载 CI 日志不重复上传；本地执行和远端 CI 不混计。collection 文件仅为收集清单。

历史记录可能包含当时在规划库路径运行的日志。这是历史留档；本次没有在规划库运行测试，也没有复制测试运行目录。归档不表示接口冻结、功能合入、独立评审完成、双 Linux、真实 kill-9、正式性能或检出率验收通过。
