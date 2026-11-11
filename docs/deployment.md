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
| `g15 consume/benchmark` | 可用 | consume #71、benchmark #92 已合入 main |
| `g15 simulate/report` | 可用 | #110 / PR #125 已合入 main，完整业务链已接通 |
| `g15 reset-alarms` | 可用 | #127 / PR #139 已合入；必须显式 `--yes` 与非空 `--reason` |
| `g15 verify/doctor` | 待接线 | Python 子命令尚未实现；不得与已可用的 `scripts/verify.sh` 混淆 |
| `verify.sh` G1–G4 | 可用 | black/pylint/pytest/validate/完整闭环；任一门禁失败整体非零 |
| `demo.sh` | 可用 | validate→simulate→consume→report，从空 `runs/` 重建闭环 |
| `benchmark.sh` | **可用（双机实测 3/3 成功）** | 百万点 production_window 三轮 0.12–0.15 s（预算 60 s），10 分片 shape 9×102400+78400，verification passed，日志留档 runs/evidence/ |

> 诚实声明：闭环与脚本可运行不等于最终检出率验收完成。benchmark 数字只覆盖生产基准口径；#56/#9 的独立保留集、#135/#136 的查询与容量门禁、soak 和断电持久性均不能由本节替代。

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

## 5. 双系统历史基线（Issue #10，2026-11-01/02/03，绑定 main `2054bb5`）

| 命令 | RHEL 9.7（实测） | Ubuntu 24.04.4（实测） |
|---|---|---|
| `setup.sh` | ✅ 退出 0（首装 + 幂等重跑，Python 3.12.16） | ✅ 退出 0（首装 + 幂等重跑，Python 3.12.16） |
| `verify.sh` G1–G3 | ✅ 全过（pylint 9.91/10；pytest 752 passed, 14 skipped；15 配置 validate 全过） | ✅ 全过（pylint 9.91/10；pytest 752 passed, 14 skipped；15 配置 validate 全过） |
| `verify.sh` G4 | ❌ 阻塞于 simulate（模块在途），按设计非零 | ❌ 同 RHEL：阻塞于 simulate，按设计非零 |
| `demo.sh` | validate ✅ → simulate 诚实失败，退出 3 | validate ✅ → simulate 诚实失败，退出 3 |
| `benchmark.sh` | ✅ 3/3 成功，production_window 0.1201/0.1262/0.1314 s（中位 0.1262，预算 60 s），verification passed | ✅ 3/3 成功，production_window 0.1279/0.1396/0.1458 s（中位 0.1396，预算 60 s），verification passed |
| `clean.sh` | ✅ 退出 0，clean 后 editable 安装复测无损 | ✅ 退出 0，clean 后 editable 安装复测无损 |

本表在 main `2054bb5`（含 #86 脚本、#90 依赖锁、#92 benchmark 实现、#85 并发用例）上实测得出，两列均为真实独立执行（RHEL 9.7 虚拟机与 Ubuntu 24.04.4 虚拟机，Conda 26.9.4 / Python 3.12.16 一致）；验证/演示证据日志留档 `~/g15-evidence/issue10-*-2054bb5-*.log`，基准轮次日志留存于两机 `runs/evidence/benchmark_Linux-x86_64_*_20261102T15*.log`（三轮原始数值见上表）。表内 benchmark 数字取自 11-02 那次双机实测；2026-11-03 公开仓库现场全新克隆轮的基准数字（RHEL 0.1315/0.1491/0.1996 s、Ubuntu 0.2721/0.2486/0.2757 s）见 #10 评论 `6582119780`。历史版本（2026-11-01，head `60fc6b4`，pytest 738 passed）的记录由本轮取代。**遗留说明**：两台虚拟机已从公开 GitHub 仓库现场全新克隆 `2054bb59` 并跑通 `setup.sh` 与 `benchmark.sh`（#10 评论 6582119780；两机 `clone_exit=0` / `setup_exit=0` / `benchmark_exit=0`、3/3 成功、verification passed，未使用代理或 bundle 中转）。仍未完成的是：`simulate`/`report` 合并后的 G4 闭环取证、双 Linux 正式验收、检出率与峰值内存结论。

### 5.1 主链接通后的双系统 G4 证据（2026-11-10）

在 `545a598c` 上，RHEL 9.7 与 Ubuntu 24.04.4 均从公开仓库全新克隆并依次执行 `setup.sh → clean.sh → demo.sh → verify.sh → benchmark.sh`，五步退出码均为 0；两机还分别执行了 opt-in abrupt-stop 压力用例（1 passed）与故障注入套件（9 passed）。原始回执见 #10 评论 `6759021839`，#10 已据此关闭。

该轮证明对应 SHA 的安装、G4 闭环、质量门禁、百万点基准与 Linux 故障用例可以在两套系统复现；它**不证明** #56/#9 的独立保留集检出率、长时间 soak、100k 查询容量或断电零丢失。`main@997acaeb` 比该 SHA 新增后续文档和 reset-alarms 变更，期末候选发布仍须在同一最终 SHA 上重跑并保存完整日志。

## 6. 双 Linux 验收记录模板（acceptance.md §1）

每台机器留存：Git 提交 SHA、configs/health_rules 版本摘要、系统版本（`/etc/os-release`）、CPU/内存/存储（`lscpu`/`free -h`/`df -h`）、Python/NumPy/Conda 版本（`pip freeze`）、每条命令与退出码、完整日志和报告文件。建议逐条 `tee` 到 `runs/evidence/`（clean.sh 会清空，归档前先移出仓库目录）。

## 7. 故障恢复与离线演示指引（acceptance.md §4）

- SHA/文件损坏隔离：篡改某个 `runs/<id>/raw/part-*.npz` 字节后重跑消费，预期被拒收并留日志（退出 3）。
- `kill -9` 恢复：消费进程运行中强杀后重启，预期从 `processed_chunks` 重放、结果与未中断运行一致。
- 断网运行：依赖预装完成后禁用网络，依次运行 `clean.sh`、`demo.sh`、`verify.sh`；预期主链不发起网络连接且结果与联网状态一致。该项在最终 SHA 尚无统一原始证据，执行前保持“待执行”。
- 对账恒等：报告同时核对 `processed + rejected = total` 与 `analysed + dropped = processed`；任一不成立即失败，不用截断或补零掩盖。
- 以上演示均保留命令、退出码、SHA、机器信息与失败日志；未实测前不得声称零数据损失或已完成现场故障验收。

## 8. 数据与设备安全边界

- 运行阶段离线；`runs/` 数据不自动外发。外发日志、报告或截图前必须脱敏并取得授权，禁止携带 token、密码、真实个人身份、真实厂区信息、私人绝对路径或 `private_truth` 原文。
- 本项目没有真实设备执行器，只输出告警。不得将告警自动转换为停机或维修指令。
- 未来接入真实设备前必须有变更单、旁路接入方案和两位非作者复核；任何停机/控制动作必须由人明确确认。
- `reset-alarms` 不是停机操作，但属于有状态运维动作，必须使用 `--yes` 和非空原因并留存输出。独立的 `audit_events` append-only 审计链已由 #143/#147 落地并通过 C4 非作者复核，应用级只增已满足；文件级防篡改仍不声称。
