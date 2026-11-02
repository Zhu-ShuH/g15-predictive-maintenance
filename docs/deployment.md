# 部署说明（Issue #10）

负责人：C6；审核人：A。目标：在 Ubuntu 24.04 LTS 与 RHEL 9 两套发行版上，从公开 GitHub 仓库全新克隆后，用本目录脚本完成安装、演示与验证（docs/interface.md §12、docs/acceptance.md §1）。

## 1. 前置条件

- 一台 Ubuntu 24.04 LTS 或 RHEL 9 的物理机/虚拟机（x86_64）。
- Conda（Miniconda 或 Anaconda）**由使用者自行安装**；脚本检测不到 Conda 时会给出安装提示并退出 2，绝不静默安装。`conda --version` 可用即可，安装位置不限（`CONDA_EXE`、`PATH`、`~/miniconda3`、`~/anaconda3`、`/opt/conda` 均可被识别）。
- 不替换操作系统 Python；全部 Python 依赖装在 Conda `pp03`（Python 3.12）内。
- 安装阶段需要联网（PyPI/Conda 下载）；**运行阶段不得依赖网络或云端 AI**。

## 2. 标准部署流程（两个发行版一致）

```sh
git clone https://github.com/Zhu-ShuH/g15-predictive-maintenance.git
cd g15-predictive-maintenance
bash scripts/setup.sh        # 创建/复用 pp03，校验 Python 3.12，安装依赖
bash scripts/demo.sh         # 闭环演示：配置验证→仿真→消费→报告（要求空 runs/）
bash scripts/verify.sh       # 质量门禁 + 测试 + 配置契约 + 闭环链路
bash scripts/benchmark.sh    # 百万点基准 ×3（结果留档 runs/evidence/）
```

脚本行为要点：

- `setup.sh`：pp03 不存在时按 `environment.yml` 创建；已存在则校验 Python 3.12，**版本不符立即退出 2 并保留原环境**，不删除、不覆盖；重复运行幂等。
- `demo.sh`：要求 `runs/` 为空（闭环结果从空目录重建）；`runs/` 非空时提示先跑 `scripts/clean.sh`。
- `verify.sh`：逐项打印 G1 质量（black/pylint≥8.5）、G2 测试（pytest）、G3 配置契约（validate 全部入库场景配置）、G4 闭环链路四道门禁的 ✅/❌，任何一项未过整体退出 4。
- `clean.sh`：只删除 `runs/`、`__pycache__`、`.pytest_cache`、`.coverage`、`*.egg-info` 等可自动重建产物，不触碰源码/文档/配置。
- 退出码约定（脚本）：0 成功；2 前置条件不满足；3 安装或命令执行失败；4 验收门禁未通过；5 意外错误。

## 3. 当前实现状态（随模块合并更新）

| 脚本/能力 | 状态 | 说明 |
|---|---|---|
| `setup.sh` | **可用（RHEL 9.7 / Ubuntu 24.04 双机实测）** | 建环境/校验版本/装依赖/导入检查 |
| `g15 validate` | 可用 | Issue #4 已实现 |
| `g15 consume` | 可用 | Issue #65/#71 已合入 main（`30ad92b`） |
| `g15 consume/benchmark` | 可用 | consume #71、benchmark #92 已合入 main |
| `g15 simulate/report/verify/doctor` | 待实现 | 接口已冻结（interface.md §10），对应模块在途 |
| `verify.sh` G1–G3 | 可用 | black/pylint/pytest/validate |
| `verify.sh` G4 | 阻塞于 simulate | consume 已可用；G4 当前先阻塞在 simulate（CLI 退出 5） |
| `demo.sh` | 部分可用 | 推进到仿真步骤诚实失败（CLI 报子命令未实现，退出 3） |
| `benchmark.sh` | **可用（双机实测 3/3 成功）** | 百万点 production_window 三轮 0.12–0.15 s（预算 60 s），10 分片 shape 9×102400+78400，verification passed，日志留档 runs/evidence/ |

> 诚实声明：simulate/report 合并前，demo 不得宣称任何闭环结论（G4 阻塞于 simulate，退出 4 是预期行为，不是脚本缺陷）；benchmark 已随 #92 可用并完成双机取证（见 §5），但其数字仅覆盖生产基准口径，不构成检出率、误报或闭环结论。

## 4. RHEL 9.7 实测记录（2026-11-01，C6）

| 项 | 值 |
|---|---|
| 系统 | Red Hat Enterprise Linux 9.7（内核默认），x86_64 虚拟机（VMware，2 vCPU / 4 GiB / 60 GiB） |
| 仓库提交 | main `0a637fc` + 本分支 `60fc6b4`（pr86-rebased-ready，本表实测所在树） |
| Conda | Miniconda 26.9.4，安装于 `~/miniconda3`（用户级，未用系统包管理器） |
| git | 2.55.0（`conda install -n base git`，系统未注册订阅、无 dnf 来源） |
| pp03 | `conda env create -f environment.yml` 创建，Python 3.12.16 |
| 验证命令 | `conda run --no-capture-output -n pp03 python --version` → `Python 3.12.16` |

RHEL 9 特有注意事项（实测遇到并已解决）：

1. **Conda 26 首次使用默认通道需接受 Anaconda ToS**，否则 `env create`/`install` 报错：
   `conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main`（`pkgs/r` 同理）。
2. **系统未注册订阅时没有 dnf 软件源**：git、编译器等系统包不可用；本项目的 git 与全部 Python 依赖均可由 Conda 提供，不依赖 dnf。
3. 手工创建 `~/.ssh/authorized_keys` 后需 `restorecon -R -v ~/.ssh` 修正 SELinux 标签，否则 sshd 公钥认证被拒（部署跳板机时相关）。
4. 网络受限环境（GitHub 直连重置）：可在宿主机代理开启"允许局域网连接"后为 git 配置 `http.proxy`，或在可信网络内用 `git bundle`/受控拷贝中转；正式验收仍须按 acceptance.md §1 从公开仓库全新克隆。

## 5. 双系统实测状态（Issue #10，2026-11-01/02 双机实测，绑定 main `2054bb5`）

| 命令 | RHEL 9.7（实测） | Ubuntu 24.04.4（实测） |
|---|---|---|
| `setup.sh` | ✅ 退出 0（首装 + 幂等重跑，Python 3.12.16） | ✅ 退出 0（首装 + 幂等重跑，Python 3.12.16） |
| `verify.sh` G1–G3 | ✅ 全过（pylint 9.91/10；pytest 752 passed, 14 skipped；15 配置 validate 全过） | ✅ 全过（pylint 9.91/10；pytest 752 passed, 14 skipped；15 配置 validate 全过） |
| `verify.sh` G4 | ❌ 阻塞于 simulate（模块在途），按设计非零 | ❌ 同 RHEL：阻塞于 simulate，按设计非零 |
| `demo.sh` | validate ✅ → simulate 诚实失败，退出 3 | validate ✅ → simulate 诚实失败，退出 3 |
| `benchmark.sh` | ✅ 3/3 成功，production_window 0.1201/0.1262/0.1314 s（中位 0.1262，预算 60 s），verification passed | ✅ 3/3 成功，production_window 0.1279/0.1396/0.1458 s（中位 0.1396，预算 60 s），verification passed |
| `clean.sh` | ✅ 退出 0，clean 后 editable 安装复测无损 | ✅ 退出 0，clean 后 editable 安装复测无损 |

本表在 main `2054bb5`（含 #86 脚本、#90 依赖锁、#92 benchmark 实现、#85 并发用例）上实测得出，两列均为真实独立执行（RHEL 9.7 虚拟机与 Ubuntu 24.04.4 虚拟机，Conda 26.9.4 / Python 3.12.16 一致）；验证/演示证据日志留档 `~/g15-evidence/issue10-*-2054bb5-*.log`，基准轮次日志留存于两机 `runs/evidence/benchmark_Linux-x86_64_*_20261102T15*.log`（三轮原始数值见上表）。历史版本（2026-11-01，head `60fc6b4`，pytest 738 passed）的记录由本轮取代。**遗留说明**：部署到两台虚拟机当前经受控 bundle 中转（GitHub 直连在 C6 网络环境受限）；acceptance.md §1 要求的「从公开 GitHub 仓库现场全新克隆」演示待网络条件满足后按同脚本补充留证。simulate/report 模块合并后，同一脚本在两系统重跑即为 G4 闭环取证。

## 6. 双 Linux 验收记录模板（acceptance.md §1）

每台机器留存：Git 提交 SHA、configs/health_rules 版本摘要、系统版本（`/etc/os-release`）、CPU/内存/存储（`lscpu`/`free -h`/`df -h`）、Python/NumPy/Conda 版本（`pip freeze`）、每条命令与退出码、完整日志和报告文件。建议逐条 `tee` 到 `runs/evidence/`（clean.sh 会清空，归档前先移出仓库目录）。

## 7. 故障恢复演示指引（acceptance.md §4，待可产出运行数据的仿真入口交付后执行）

- SHA/文件损坏隔离：篡改某个 `runs/<id>/raw/part-*.npz` 字节后重跑消费，预期被拒收并留日志（退出 3）。
- `kill -9` 恢复：消费进程运行中强杀后重启，预期从 `processed_chunks` 重放、结果与未中断运行一致。
- 以上两项演示均保留命令与日志；未实测前不得声称零数据损失。
