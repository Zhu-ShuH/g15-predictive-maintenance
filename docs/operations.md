# 运维说明（Issue #10）

日常运行、排障与证据留存。配合 `docs/deployment.md` 与 `docs/environment.md` 使用。

## 1. 日常运行

```sh
conda activate pp03                 # 交互式使用；脚本内部一律用 conda run，不依赖激活
python -m g15 validate --config configs/default.yaml
bash scripts/demo.sh                # 闭环演示（要求空 runs/）
bash scripts/verify.sh              # 门禁自检
bash scripts/benchmark.sh           # 百万点三轮基准与独立校验
bash scripts/clean.sh               # 清理自动产物（不碰源码/配置/文档）
```

运行产物统一在 `runs/<run_id>/`：`manifest.json`、`raw/`、`artifacts/results.sqlite`、`reports/summary.{json,md}`、`logs/app.jsonl`（docs/interface.md §11）。

## 2. 退出码速查

| 退出码 | CLI（interface.md §10） | scripts/*.sh |
|---|---|---|
| 0 | 成功 | 成功 |
| 2 | 参数/配置不合法 | 前置条件不满足（Conda 缺失 / pp03 版本不符 / runs 非空） |
| 3 | I/O 或 Schema 验证失败 | 安装或命令执行失败 |
| 4 | 验收门禁失败 | verify.sh 有门禁未通过 |
| 5 | 未预期错误 | 意外错误 |

## 3. 常见故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `setup.sh: 未找到 Conda` | Conda 未安装或不在 `PATH`/常见位置 | 自行安装 Miniconda；或 `export CONDA_EXE=/路径/conda` 后重试。脚本不会替你装 Conda |
| `pp03 的 Python 版本为 …，不符合 3.12` | 环境是旧课程环境 | 按规范**不要删除**环境；把版本结果交 A/C6 协调迁移 |
| `conda env create` 报 ToS/通道错误 | Conda ≥26 要求接受 Anaconda ToS | `conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main`（`pkgs/r` 同理）后重试 |
| `pip install` 网络失败 | 安装阶段网络受限 | 换可信网络或配置镜像后重跑 `setup.sh`；运行阶段仍须离线可用 |
| `git clone` 连接被重置 | 网络到 GitHub 不通 | 用代理（宿主机代理开 Allow LAN 后为 git 配置 `http.proxy`）或在可信网络重新克隆；中转拷贝不作为正式验收克隆 |
| `demo.sh: runs/ 目录非空` | 上次运行残留 | `bash scripts/clean.sh` 后重跑；demo 不删用户数据 |
| `verify.sh` G4 失败 | 当前树的 simulate/consume/report、配置或运行目录异常 | 保存四道门禁原始输出并按第一处非零命令排查；主线已经接通，不能再把“子命令未实现”当作预期 |
| `black/pylint/pytest` 失败 | 代码改动破坏质量门禁 | 按工具输出修复；门禁阈值见 pyproject/verify.sh，不得为过关放宽 |
| 消费重复入库 / kill -9 后数据异常 | 消费事务或重放缺陷 | 保留 `runs/<id>/logs/app.jsonl` 与 SQLite，按 interface.md §9 契约定位，勿手工改库 |
| `reset-alarms` 拒绝执行 | 未提供 `--yes`/非空原因、设备不匹配或无存量状态 | 核对 run/device/rule，记录原因后显式确认；禁止在消费失败时自动重置 |

## 4. 证据留存（acceptance.md §1）

- 每次验收/演示逐条 `tee` 命令日志；留存提交 SHA、系统/硬件/版本信息、退出码。
- 基准日志由 `benchmark.sh` 自动写入 `runs/evidence/benchmark_*.log`；**归档前先复制到仓库目录之外**，`clean.sh` 会清空 `runs/`。
- 日志与报告中不得出现机器绝对私人路径、令牌或密钥（interface.md §11）。

## 5. 安全与边界

- 脚本全部以仓库根为工作目录，使用相对路径定位，不写死机器路径。
- 运行阶段禁止访问网络与云端 AI；安装阶段（setup/pip/conda）除外。
- `clean.sh` 的删除清单是封闭集合；新增可清理产物必须走 Issue 评审，不得随手加。
- 当前主链不提供账号登录，也不持久化密码；禁止在配置、源码、日志和证据中写明文密码。未来若增加口令，先评审契约并采用带随机盐的 PBKDF2/等价强哈希和恒时比较。
- 工业运行数据默认不出本机。外发报告、日志、SQLite、截图或样例前，提交授权记录并脱敏 token、个人身份、真实厂区信息、私人绝对路径和 `private_truth` 原文。
- 本项目只告警、没有设备执行器，不得自动停机。未来真实设备接入必须有变更单、旁路接入和两位非作者复核；停机/控制动作必须人工确认。
- `reset-alarms` 必须显式 `--yes` 和非空 `--reason`；它保留旧状态供追溯，但现有行内历史并非独立 append-only 安全审计，修复完成前不得宣称满足不可改审计红线。

## 6. 显式告警状态重置

```sh
python -m g15 reset-alarms \
  --run-dir runs/<run_id> \
  --device-id <device_id> \
  --rule-id health_heuristic \
  --reason "经人工复核后的重置原因" \
  --yes
```

重置只清空 streak 并保留消费进度，不删除历史告警、不重新分析 run，也不会阻止下一窗口再次告警。命令输出、操作者、原因、时间与目标 run 应一并归档；失败时不绕过确认或手工改库。

## 7. 期末现场故障演练矩阵

所有用例先记录候选 SHA、系统版本、配置摘要和开始时间；失败也保留。教师现场输入与仓库固定用例分开标识。

| 场景 | 操作 | 通过判据 | 当前证据状态 |
|---|---|---|---|
| 断网 | 完成安装后禁用网络，运行 `clean.sh → demo.sh → verify.sh` | 主链无联网依赖，命令退出 0，产物完整 | **最终 SHA 待执行**；设计上离线不等于已有实测 |
| 坏数据 | 修改一份已发布 NPZ 的字节后消费 | SHA/Schema 拒收、退出非零、不写假结果 | 自动化与历史证据已有；最终 SHA 需现场复跑 |
| kill 进程 | 首个分片提交后对消费者发 `SIGKILL`，再重启 | 中间态是真实部分前缀；重放后五表逐行等于未中断对照 | 双 Linux 历史证据已有；最终 SHA 需现场复跑 |
| 幂等 | 同一 run 重复消费 | 第二次不新增窗口、告警或分片记录 | 自动化已有；现场保存表计数 |
| 对账恒等 | 生成报告并读取计数 | `processed+rejected=total` 且 `analysed+dropped=processed` | 自动化已有；现场保存 JSON/Markdown |
| 百万点 | 运行 `benchmark.sh` 三轮 | 每轮 1,000,000 点、10 分片、独立校验通过且生产窗口 <60s | 双 Linux 历史证据已有；最终 SHA 需重跑 |
| 查询 p95/EXPLAIN | 完成 #135 后执行 #136 Gate 0 | p50/p95/p99、查询计划、资源与 oracle 对账齐 | **未实现/未执行**，不得现场冒充 |

## 8. 现场回退

若完整演示失败，不修改门槛或删除失败证据：保留失败目录和日志，切换到已核验的冻结 tag/SHA 重新 clean clone，按部署说明逐条执行。录屏、截图或旧日志只能辅助定位，不能替代当场可运行仓库。
