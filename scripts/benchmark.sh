#!/usr/bin/env bash
# benchmark.sh —— 百万点性能基准，生成可审计结果（docs/acceptance.md §2）。
#
# 约定：
#   * 使用入库的 configs/benchmark_1m.yaml（固定 seed=42、1e6 点、chunk_points=102400），
#     禁止临场改数据规模；计时口径由 g15 benchmark 命令内部按 acceptance.md §2 执行
#     （perf_counter，从配置校验后到 complete Manifest 发布，CLI 总墙钟另列）。
#   * 独立空运行目录、连续 3 次、保留全部输出到 runs/ 下的日志文件；
#     显式传入 --max-production-seconds 60，由 CLI 用未舍入的计时判定。
#
# 退出码：0 三次均通过时间/完整性门禁且日志成功；2 前置不满足；3 任一轮或日志失败。
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
EVIDENCE_DIR="$(mktemp -d "runs/evidence/benchmark_${HOST_TAG//\//_}_${TS}_XXXXXX")" \
  || die "无法创建独立基准证据目录" 3
printf 'run\tcommand_exit\ttee_exit\tcommand\n' > "$EVIDENCE_DIR/steps.tsv" \
  || die "无法写入基准步骤记录" 3
echo "[benchmark] 系统: $HOST_TAG | 开始时间(UTC): $TS | 连续 3 次独立空运行"

FAIL=0
for i in 1 2 3; do
  LOG="$EVIDENCE_DIR/run${i}.log"
  echo "=== 第 $i/3 次（日志: $LOG）==="
  # Capture both codes immediately. A failed run or tee never hides later rounds.
  set +e
  run_py -m g15 benchmark --config "$BENCH_CONFIG" --max-production-seconds 60 2>&1 | tee "$LOG"
  PIPE_CODES=("${PIPESTATUS[@]}")
  set -e
  command_rc="${PIPE_CODES[0]}"
  tee_rc="${PIPE_CODES[1]}"
  echo "[benchmark] 第 $i 次 command_exit=$command_rc tee_exit=$tee_rc"
  if ! printf '%s\t%s\t%s\t%s\n' "$i" "$command_rc" "$tee_rc" \
      'python -m g15 benchmark --config configs/benchmark_1m.yaml --max-production-seconds 60' \
      >> "$EVIDENCE_DIR/steps.tsv"; then
    echo "[benchmark] 第 $i 次退出码记录写入失败" >&2
    FAIL=$((FAIL+1))
  fi
  if [ "$command_rc" -eq 0 ] && [ "$tee_rc" -eq 0 ]; then
    echo "[benchmark] 第 $i 次时间与完整性门禁通过"
  else
    echo "[benchmark] 第 $i 次失败（command=$command_rc, tee=$tee_rc）；原始输出目标 $LOG" >&2
    FAIL=$((FAIL+1))
  fi
done

if [ "$FAIL" -ne 0 ]; then
  die "存在 $FAIL 项基准或证据写入失败；不得宣称三轮性能通过，证据 $EVIDENCE_DIR" 3
fi
echo "[benchmark] 3 次均严格小于60秒且完整性校验通过；证据 $EVIDENCE_DIR"
