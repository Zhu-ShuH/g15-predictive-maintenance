# 运维说明（Issue #10）

日常运行、排障与证据留存。配合 `docs/deployment.md` 与 `docs/environment.md` 使用。

## 1. 日常运行

```sh
conda activate pp03                 # 交互式使用；脚本内部一律用 conda run，不依赖激活
python -m g15 validate --config configs/default.yaml
bash scripts/demo.sh                # 闭环演示（要求空 runs/）
bash scripts/verify.sh              # 门禁自检
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
| `verify.sh` G4 报"子命令未实现" | simulate/report 等模块未合并（consume 已于 #71 合入） | 属预期：G4 当前阻塞在 simulate；等待对应 Issue 合并，无需改脚本 |
| `black/pylint/pytest` 失败 | 代码改动破坏质量门禁 | 按工具输出修复；门禁阈值见 pyproject/verify.sh，不得为过关放宽 |
| 消费重复入库 / kill -9 后数据异常 | 消费事务或重放缺陷 | 保留 `runs/<id>/logs/app.jsonl` 与 SQLite，按 interface.md §9 契约定位，勿手工改库 |

## 4. 证据留存（acceptance.md §1）

- 每次验收/演示逐条 `tee` 命令日志；留存提交 SHA、系统/硬件/版本信息、退出码。
- 基准日志由 `benchmark.sh` 自动写入 `runs/evidence/benchmark_*.log`；**归档前先复制到仓库目录之外**，`clean.sh` 会清空 `runs/`。
- 日志与报告中不得出现机器绝对私人路径、令牌或密钥（interface.md §11）。

## 5. 安全与边界

- 脚本全部以仓库根为工作目录，使用相对路径定位，不写死机器路径。
- 运行阶段禁止访问网络与云端 AI；安装阶段（setup/pip/conda）除外。
- `clean.sh` 的删除清单是封闭集合；新增可清理产物必须走 Issue 评审，不得随手加。
