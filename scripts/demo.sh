#!/usr/bin/env bash
# demo.sh —— 闭环演示：配置验证 → 仿真 → 消费 → 报告（docs/interface.md §10/§12）。
#
# 约定：
#   * 使用 configs/sudden_fault.yaml 作为演示场景，从空的 runs/ 目录重建结果；
#     runs/ 非空时拒绝执行（清理属于 clean.sh，本脚本不删用户数据）。
#   * 通过 python -m g15 驱动链路；某一步未实现时按 CLI 契约非零退出，
#    本脚本不做任何"假装成功"的降级（docs/development.md：不允许脚本"看上去成功"）。
#
# 退出码：0 成功；2 前置条件不满足（Conda/pp03/runs 非空）；
#         3 链路命令执行失败（含 CLI 报"子命令未实现"）；5 意外错误。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

die() { printf 'demo.sh: %s\n' "$1" >&2; exit "${2:-5}"; }

# ---- Conda / pp03 前置检查（与 setup.sh 相同口径）----
CONDA="${CONDA_EXE:-}"
if [ -z "$CONDA" ] || [ ! -x "$CONDA" ]; then
  if command -v conda >/dev/null 2>&1; then
    CONDA="$(command -v conda)"
  elif [ -x "$HOME/miniconda3/bin/conda" ]; then
    CONDA="$HOME/miniconda3/bin/conda"
  elif [ -x "$HOME/anaconda3/bin/conda" ]; then
    CONDA="$HOME/anaconda3/bin/conda"
  elif [ -x /opt/conda/bin/conda ]; then
    CONDA=/opt/conda/bin/conda
  else
    die "未找到 Conda；请先运行 scripts/setup.sh 的环境准备（见 docs/environment.md）" 2
  fi
fi
PYVER="$("$CONDA" run --no-capture-output -n pp03 python -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
  || die "pp03 不可用；请先运行 bash scripts/setup.sh" 2
case "$PYVER" in
  3.12.*) : ;;
  *) die "pp03 Python 版本 $PYVER 不符合 3.12 约定" 2 ;;
esac
run_py() { "$CONDA" run --no-capture-output -n pp03 python "$@"; }

# ---- runs/ 必须为空：闭环结果要从空目录重建 ----
mkdir -p runs
if [ -n "$(ls -A runs 2>/dev/null)" ]; then
  die "runs/ 目录非空；闭环演示要求从空 runs/ 重建。请先运行 bash scripts/clean.sh" 2
fi

DEMO_CONFIG="configs/sudden_fault.yaml"
[ -f "$DEMO_CONFIG" ] || die "缺少演示配置 $DEMO_CONFIG" 2

echo "[demo 1/4] 配置验证: g15 validate --config $DEMO_CONFIG"
run_py -m g15 validate --config "$DEMO_CONFIG" || die "配置验证失败（退出码 $?）" 3

echo "[demo 2/4] 仿真生产: g15 simulate --config $DEMO_CONFIG --output-root runs"
run_py -m g15 simulate --config "$DEMO_CONFIG" --output-root runs || die "仿真失败（退出码 $?）；若提示 not implemented，说明对应模块尚未合并" 3

RUN_DIR="$(find runs -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -n 1)"
[ -n "$RUN_DIR" ] || die "simulate 未产出运行目录" 3
echo "[demo 2/4] 运行目录: $RUN_DIR"

echo "[demo 3/4] 独立消费: g15 consume --run-dir $RUN_DIR --config configs/consumer.yaml"
run_py -m g15 consume --run-dir "$RUN_DIR" --config configs/consumer.yaml || die "消费失败（退出码 $?）" 3

echo "[demo 4/4] 生成报告: g15 report --run-dir $RUN_DIR"
run_py -m g15 report --run-dir "$RUN_DIR" || die "报告生成失败（退出码 $?）" 3

echo "[demo] 闭环完成。报告: $RUN_DIR/reports/summary.json 与 summary.md"
echo "[demo] 验收门禁请运行: bash scripts/verify.sh"
