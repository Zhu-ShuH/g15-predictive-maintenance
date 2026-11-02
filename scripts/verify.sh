#!/usr/bin/env bash
# verify.sh —— 检查测试、完整链路和验收门禁（docs/interface.md §12、docs/acceptance.md）。
#
# 门禁：
#   G1 质量门禁   black --check src tests；pylint src/g15 --fail-under=8.5
#   G2 测试门禁   pytest -q（单元/集成，覆盖全部已实现模块）
#   G3 配置契约   g15 validate 校验全部入库场景配置（configs/*.yaml，除 consumer.yaml）
#   G4 闭环链路   独立临时目录执行 demo 链路（simulate→consume→report），
#                 模块未合并时该门禁明确报"待实现"，整体非零退出。
#
# 诚实原则：任何门禁未通过整体即非零退出，并逐项打印 ✅/❌ 状态；
# 不用部分通过冒充完整验收（docs/acceptance.md §4）。
#
# 退出码：0 全部门禁通过；4 有门禁未通过；2 前置条件不满足；5 意外错误。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

die() { printf 'verify.sh: %s\n' "$1" >&2; exit "${2:-5}"; }

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
echo "[verify] pp03 Python $PYVER"

FAILURES=0

# ---- G1 质量门禁 ----
echo "=== G1 质量门禁: black / pylint ==="
if run_py -m black --check src tests; then echo "✅ G1a black --check 通过"; else echo "❌ G1a black 未通过"; FAILURES=$((FAILURES+1)); fi
if run_py -m pylint src/g15 --fail-under=8.5; then echo "✅ G1b pylint ≥8.5 通过"; else echo "❌ G1b pylint 未达标"; FAILURES=$((FAILURES+1)); fi

# ---- G2 测试门禁 ----
echo "=== G2 测试门禁: pytest ==="
if run_py -m pytest -q; then echo "✅ G2 pytest 通过"; else echo "❌ G2 pytest 未通过"; FAILURES=$((FAILURES+1)); fi

# ---- G3 配置契约门禁 ----
echo "=== G3 配置契约: g15 validate 全部入库场景配置 ==="
G3_FAIL=0
for cfg in configs/*.yaml; do
  base="$(basename "$cfg")"
  # consumer.yaml 是消费配置、health_rules.yaml 是评分规则，都不是场景配置，不走 validate
  [ "$base" = "consumer.yaml" ] && continue
  [ "$base" = "health_rules.yaml" ] && continue
  if run_py -m g15 validate --config "$cfg" > /dev/null 2>&1; then
    echo "✅ validate $base"
  else
    echo "❌ validate $base"
    G3_FAIL=$((G3_FAIL+1))
  fi
done
if [ "$G3_FAIL" -eq 0 ]; then echo "✅ G3 配置契约全部通过"; else echo "❌ G3 有 $G3_FAIL 个配置校验失败"; FAILURES=$((FAILURES+1)); fi

# ---- G4 闭环链路门禁（独立临时目录，不碰用户 runs/ 数据）----
echo "=== G4 闭环链路: simulate→consume→report（独立临时目录）==="
VERIFY_RUNS="runs/.verify-$$"
mkdir -p "$VERIFY_RUNS"
G4_NOTE=""
if run_py -m g15 simulate --config configs/sudden_fault.yaml --output-root "$VERIFY_RUNS" > /dev/null 2>&1; then
  RUN_DIR="$(find "$VERIFY_RUNS" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
  if run_py -m g15 consume --run-dir "$RUN_DIR" --config configs/consumer.yaml > /dev/null 2>&1 \
     && run_py -m g15 report --run-dir "$RUN_DIR" > /dev/null 2>&1; then
    echo "✅ G4 闭环链路通过（$RUN_DIR）"
  else
    echo "❌ G4 消费或报告阶段失败"; FAILURES=$((FAILURES+1))
  fi
else
  rc=$?
  echo "❌ G4 仿真阶段未通过（退出码 $rc）；若为 CLI 报子命令未实现，则该门禁待模块合并后自动生效"
  G4_NOTE="G4 当前先依赖尚未实现的 simulate，后续还需 report；consume 已由 #71 合入，当前计数为未通过门禁"
  FAILURES=$((FAILURES+1))
fi
rm -rf "$VERIFY_RUNS"

# ---- 汇总 ----
echo "=========================================="
if [ "$FAILURES" -eq 0 ]; then
  echo "verify.sh: 全部门禁通过 ✅"
  exit 0
fi
echo "verify.sh: ${FAILURES} 项门禁未通过 ❌"
[ -n "$G4_NOTE" ] && printf '%s\n' "备注: $G4_NOTE" >&2
exit 4
