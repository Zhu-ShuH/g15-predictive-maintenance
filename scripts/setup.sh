#!/usr/bin/env bash
# setup.sh —— 按 docs/environment.md 创建或复用 Conda pp03 并安装项目依赖。
#
# 行为（docs/environment.md §2/§3、docs/interface.md §12）：
#   1. 检查 Conda；缺失时给出清晰安装提示并非零退出，绝不静默安装 Anaconda/Miniconda。
#   2. pp03 不存在时按 environment.yml 创建；已存在时检查 Python 3.12，
#      版本不符立即停止并说明原因；重复运行不删除、不覆盖已有环境。
#   3. 版本确认后才安装，且**分两条**命令，与 docs/environment.md §2 和
#      .github/workflows/ci.yml 的 skeleton job 逐条对应：
#        ① pip install -r requirements-dev.txt（间接安装 requirements.lock.txt）
#        ② pip install -e . --no-deps --no-build-isolation
#      随后执行 pip check 核对已装版本集合与锁一致。
#      两条必须拆开：--no-build-isolation 要求构建后端 setuptools.build_meta
#      已在环境中就位，而 setuptools 在锁内、只能由第 ① 条装上；把两个开关
#      直接加在原来那一条命令后面会以 BackendUnavailable 失败。
#   4. 最后做 g15/numpy/yaml 导入检查。
#
# 退出码：0 成功；2 前置条件不满足（Conda 缺失 / pp03 版本不符 / 文件缺失）；
#         3 安装或命令执行失败。
set -euo pipefail

# ---- 定位仓库根（仅用相对路径，禁止写死机器路径）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

die() { printf 'setup.sh: %s\n' "$1" >&2; exit "${2:-3}"; }

# ---- 1. 查找 Conda：CONDA_EXE > PATH > 常见安装位置 ----
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
    die "未找到 Conda。请先安装 Miniconda/Anaconda 并确认 \`conda --version\` 可用后重试。
  参考安装（不由本脚本自动执行）: https://docs.anaconda.com/miniconda/" 2
  fi
fi
echo "[setup] Conda: $($CONDA --version 2>/dev/null || "$CONDA" -V)"

# ---- 2. 记录环境要素（docs/environment.md §3：发行版/版本/来源/架构）----
OS_ID="$(. /etc/os-release 2>/dev/null && printf '%s %s' "${ID:-unknown}" "${VERSION_ID:-}" || echo unknown-os)"
echo "[setup] 系统: $OS_ID | 架构: $(uname -m)"

# ---- 3. pp03 存在性检查：不存在则创建，存在则校验 Python 3.12 ----
if ! "$CONDA" env list 2>/dev/null | grep -qE '(^|[[:space:]])pp03([[:space:]]|$)'; then
  echo "[setup] 未发现 pp03，按 environment.yml 创建..."
  "$CONDA" env create -f environment.yml || die "conda env create 失败，见上方输出" 3
else
  echo "[setup] 已存在 pp03，跳过创建（重复运行不删除环境）"
fi

PYVER="$("$CONDA" run --no-capture-output -n pp03 python -c 'import platform; print(platform.python_version())')" \
  || die "无法在 pp03 中运行 python，请检查环境完整性" 3
case "$PYVER" in
  3.12.*) echo "[setup] pp03 Python 版本: $PYVER（符合 3.12 约定）" ;;
  *) die "pp03 的 Python 版本为 $PYVER，不符合 3.12 约定。按规范保留现有环境，联系 A/C6 协调迁移，本脚本不覆盖。" 2 ;;
esac

# ---- 4. 安装锁定的依赖，再安装项目（版本确认后才执行）----
# 与 docs/environment.md §2 和 CI 的 skeleton job 逐条对应：先装锁、再装项目。
echo "[setup] ① 安装锁定依赖: pip install -r requirements-dev.txt"
"$CONDA" run --no-capture-output -n pp03 python -m pip install -r requirements-dev.txt \
  || die "锁定依赖安装失败（网络或依赖冲突），见上方 pip 输出" 3

echo "[setup] ② 安装项目（editable，锁定口径，不带依赖解析与构建隔离）: pip install -e . --no-deps --no-build-isolation"
"$CONDA" run --no-capture-output -n pp03 python -m pip install -e . --no-deps --no-build-isolation \
  || die "项目安装失败（网络或构建问题），见上方 pip 输出" 3

# ---- 5. 核对已装版本集合与锁一致（CI 的第三步）----
echo "[setup] ③ 核对已装版本集合: pip check"
"$CONDA" run --no-capture-output -n pp03 python -m pip check \
  || die "pip check 报告依赖冲突（见上方输出）；已装版本集合与锁不一致" 3

# ---- 6. 导入检查 ----
echo "[setup] 导入检查..."
"$CONDA" run --no-capture-output -n pp03 python -c "import g15, numpy, yaml; print('G15 package imports OK')" \
  || die "g15/numpy/yaml 导入失败" 3

echo "[setup] 完成：pp03（Python $PYVER）就绪，依赖已安装。"
echo "[setup] 后续可用: bash scripts/demo.sh | scripts/verify.sh | scripts/benchmark.sh"
