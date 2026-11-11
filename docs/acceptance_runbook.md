# G15 现场一键验收运行手册

> 对应 Issue #144。本手册只描述公开、可复现的运行步骤，不替代
> `docs/acceptance.md` 的业务判据，也不包含真实账号、主机、网络或厂区信息。

## 1. 正式运行

先按 `scripts/setup.sh` 完成依赖安装。正式验收必须使用无未提交/未跟踪文件的
候选提交，在仓库根执行：

```sh
mkdir -p "$HOME/g15-evidence"
G15_ACCEPTANCE_EVIDENCE_ROOT="$HOME/g15-evidence" \
  bash scripts/acceptance.sh
```

证据根必须位于业务仓库、Obsidian 规划库及其 `output/` 之外。脚本不安装或下载
依赖；它从固定 `source_sha` 执行 `git archive`，在系统 `mktemp` 目录建立受测副本，
并把 pytest 临时目录放到同一外部临时根。源仓库的 `runs/` 不承载本轮产物。

## 2. 门禁与退出码

总入口顺序执行：

1. `scripts/verify.sh`：Black、Pylint≥8.5、全量 pytest、配置和闭环；
2. 离线自检、坏数据与进程中断故障集合；
3. `scripts/benchmark.sh`：百万点三轮基准。

任一门禁失败，总入口退出 4；前置条件或路径错误退出 2；验收框架、临时目录或
日志写入失败退出 5。每个子命令与 `tee` 的原始退出码仍写入 `steps.tsv`。

## 3. 证据判定

可绑定候选提交的一轮必须同时满足：

- `overall_exit_code=0`；
- `source_worktree_dirty=false`；
- `source_snapshot_mode=git_archive`；
- `formal_evidence_eligible=true`；
- `source_sha`、`source_tree`、`snapshot_manifest_sha256` 与待评审候选一致；
- `steps.tsv` 中 `verify`、`fault_injection`、`benchmark` 的命令和 tee 退出码均为 0。

证据目录还包含环境、完整步骤日志和 benchmark 原始日志。长期归档前应做秘密、
个人路径和身份信息扫描；不得把受限数据放入公开 PR 或普通聊天附件。

## 4. 开发预检与平台边界

未提交实现只能显式执行：

```sh
G15_ACCEPTANCE_ALLOW_DIRTY=1 bash scripts/acceptance.sh
```

该模式会在 stderr 醒目告警，并固定写
`source_snapshot_mode=dirty_worktree`、`formal_evidence_eligible=false`；即使退出 0，
也不能作为最终 SHA 证据。

Python 离线守卫会先证明 DNS 与 AF_INET socket 调用被拒绝，再运行真实小型
`validate → simulate → consume → report`。它只覆盖 Python socket 审计事件，不等于
操作系统断网，也不能排除绕过审计 API 的原生代码。最终 SHA 仍须在关闭网卡或
隔离网络命名空间后复跑。Windows 没有 POSIX `SIGKILL`，该用例会明确 skip；正式
强杀恢复证据必须来自 Ubuntu 与 RHEL。本入口也不执行 #56 的未公开保留集，退出 0
不能替代 #56→#9 的独立效果验收。
