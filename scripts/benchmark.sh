#!/usr/bin/env bash
# benchmark.sh —— 百万点性能基准，生成可审计结果（docs/acceptance.md §2）。
#
# 约定：
#   * 使用入库的 configs/benchmark_1m.yaml（固定 seed=42、1e6 点、chunk_points=102400），
#     禁止临场改数据规模；计时口径由 g15 benchmark 命令内部按 acceptance.md §2 执行
#     （perf_counter，从配置校验后到 complete Manifest 发布，CLI 总墙钟另列）。
#   * 独立空运行目录、连续 3 次、保留全部输出到 runs/ 下的日志文件；
#     3 次均须 <60 秒才算达标，由 g15 benchmark 的结果与日志共同证明。
#
# 退出码：0 三次全部成功；2 前置条件不满足；3 基准执行失败（含模块未实现）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

die() { printf 'benchmark.sh: %s\n' "$1" >&2; exit "${2:-5}"; }

# ---- Conda / pp03 前置检查 ----
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
    die "未找到 Conda；请先运行 scripts/setup.sh 的环境准备" 2
  fi
fi
PYVER="$("$CONDA" run --no-capture-output -n pp03 python -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
  || die "pp03 不可用；请先运行 bash scripts/setup.sh" 2
case "$PYVER" in
  3.12.*) : ;;
  *) die "pp03 Python 版本 $PYVER 不符合 3.12 约定" 2 ;;
esac
run_py() { "$CONDA" run --no-capture-output -n pp03 python "$@"; }

BENCH_CONFIG="configs/benchmark_1m.yaml"
[ -f "$BENCH_CONFIG" ] || die "缺少基准配置 $BENCH_CONFIG" 2

mkdir -p runs/evidence
TS="$(date -u +%Y%m%dT%H%M%SZ)"
HOST_TAG="$(uname -s)-$(uname -m)"
echo "[benchmark] 系统: $HOST_TAG | 开始时间(UTC): $TS | 连续 3 次独立空运行"

FAIL=0
for i in 1 2 3; do
  LOG="runs/evidence/benchmark_${HOST_TAG//\//_}_run${i}_${TS}.log"
  echo "=== 第 $i/3 次（日志: $LOG）==="
  if run_py -m g15 benchmark --config "$BENCH_CONFIG" 2>&1 | tee "$LOG"; then
    echo "[benchmark] 第 $i 次完成"
  else
    rc=$?
    echo "[benchmark] 第 $i 次失败（退出码 $rc），已保留日志 $LOG" >&2
    FAIL=$((FAIL+1))
  fi
done

if [ "$FAIL" -ne 0 ]; then
  die "$FAIL/3 次基准失败；若提示子命令未实现，说明性能模块尚未合并，不得宣称任何性能结论" 3
fi
echo "[benchmark] 3 次基准全部成功；日志在 runs/evidence/，请随验收材料留存"
