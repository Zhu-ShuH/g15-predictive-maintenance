#!/usr/bin/env bash
# acceptance.sh -- dependency-preinstalled, fail-closed on-site acceptance entry.
#
# The source worktree is copied to an OS temporary directory before any gate
# runs. verify.sh and benchmark.sh may therefore use their historical runs/
# paths without writing test artefacts into the source repository. Durable
# logs are kept in a separate external evidence directory.
#
# Exit codes: 0 all steps passed; 2 prerequisite/path failure; 4 an acceptance
# gate failed; 5 the harness/evidence writer failed. Raw child exit codes stay
# in steps.tsv. No dependency installation occurs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

die() { printf 'acceptance.sh: %s\n' "$1" >&2; exit "${2:-5}"; }

is_within() {
  case "$1/" in
    "$2/"*) return 0 ;;
    *) return 1 ;;
  esac
}

is_in_obsidian_vault() {
  local candidate="$1"
  while [ "$candidate" != "/" ] && [ -n "$candidate" ]; do
    if [ -d "$candidate/.obsidian" ]; then
      return 0
    fi
    candidate="$(dirname "$candidate")"
  done
  return 1
}

# Dependencies are a prerequisite. This script never calls pip/conda install.
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
    die "未找到 Conda；请先完成 scripts/setup.sh，现场验收阶段不安装依赖" 2
  fi
fi

PYVER="$("$CONDA" run --no-capture-output -n pp03 python -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
  || die "pp03 不可用；请先完成 scripts/setup.sh" 2
case "$PYVER" in
  3.12.*) : ;;
  *) die "pp03 Python 版本 $PYVER 不符合 3.12 约定" 2 ;;
esac

for REQUIRED_TOOL in git tar mktemp sha256sum find sort xargs awk tee; do
  command -v "$REQUIRED_TOOL" >/dev/null 2>&1 \
    || die "缺少现场验收工具: $REQUIRED_TOOL" 2
done

SOURCE_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)" \
  || die "无法读取受测 Git SHA" 2
SHORT_SHA="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SOURCE_STATUS="$(git -C "$REPO_ROOT" status --porcelain)" \
  || die "无法读取受测工作树状态" 2
ALLOW_DIRTY="${G15_ACCEPTANCE_ALLOW_DIRTY:-0}"
case "$ALLOW_DIRTY" in
  0|1) : ;;
  *) die "G15_ACCEPTANCE_ALLOW_DIRTY 只允许 0 或 1" 2 ;;
esac
if [ -n "$SOURCE_STATUS" ]; then
  SOURCE_WORKTREE_DIRTY=true
  FORMAL_EVIDENCE_ELIGIBLE=false
  SOURCE_SNAPSHOT_MODE=dirty_worktree
  [ "$ALLOW_DIRTY" = "1" ] \
    || die "工作树不干净；正式验收只接受固定提交。开发预检可显式设置 G15_ACCEPTANCE_ALLOW_DIRTY=1" 2
  printf '%s\n' \
    'acceptance.sh: WARNING: 脏工作树开发预检；输出不是正式 SHA 验收证据' >&2
else
  SOURCE_WORKTREE_DIRTY=false
  FORMAL_EVIDENCE_ELIGIBLE=true
  SOURCE_SNAPSHOT_MODE=git_archive
fi
SOURCE_TREE="$(git -C "$REPO_ROOT" rev-parse "${SOURCE_SHA}^{tree}")" \
  || die "无法读取受测 Git tree" 2

TEMP_PARENT="${TMPDIR:-/tmp}"
[ -d "$TEMP_PARENT" ] || die "临时目录根不存在: $TEMP_PARENT" 2
TEMP_PARENT="$(cd "$TEMP_PARENT" && pwd -P)"
if is_within "$TEMP_PARENT" "$REPO_ROOT"; then
  die "TMPDIR 必须位于仓库外: $TEMP_PARENT" 2
fi
if is_in_obsidian_vault "$TEMP_PARENT"; then
  die "TMPDIR 不得位于 Obsidian 规划库内: $TEMP_PARENT" 2
fi

EVIDENCE_PARENT="${G15_ACCEPTANCE_EVIDENCE_ROOT:-$TEMP_PARENT}"
[ -d "$EVIDENCE_PARENT" ] \
  || die "证据目录根不存在；请先在仓库和规划库之外创建: $EVIDENCE_PARENT" 2
EVIDENCE_PARENT="$(cd "$EVIDENCE_PARENT" && pwd -P)"
if is_within "$EVIDENCE_PARENT" "$REPO_ROOT"; then
  die "证据目录必须位于仓库外: $EVIDENCE_PARENT" 2
fi
if is_in_obsidian_vault "$EVIDENCE_PARENT"; then
  die "证据目录不得位于 Obsidian 规划库内: $EVIDENCE_PARENT" 2
fi

EVIDENCE_DIR="$(mktemp -d "$EVIDENCE_PARENT/g15-acceptance-${SHORT_SHA}.XXXXXX")" \
  || die "无法创建仓库外证据目录" 2
WORK_PARENT="$(mktemp -d "$TEMP_PARENT/g15-acceptance-work.XXXXXX")" \
  || die "无法创建仓库外工作目录" 2
SANDBOX_ROOT="$WORK_PARENT/repository"
RUNTIME_TMP="$WORK_PARENT/runtime-tmp"
mkdir -p "$SANDBOX_ROOT" "$RUNTIME_TMP"
touch "$WORK_PARENT/.g15-acceptance-owned"

if is_within "$WORK_PARENT" "$REPO_ROOT"; then
  die "工作目录必须位于仓库外: $WORK_PARENT" 2
fi
if is_within "$EVIDENCE_DIR" "$REPO_ROOT"; then
  die "证据目录必须位于仓库外: $EVIDENCE_DIR" 2
fi

FINALIZED=0
finalize() {
  local status=$?
  if [ "$FINALIZED" -eq 0 ]; then
    FINALIZED=1
    if [ -d "$SANDBOX_ROOT/runs/evidence" ]; then
      if ! mkdir -p "$EVIDENCE_DIR/benchmark-artifacts"; then
        printf 'acceptance.sh: benchmark 证据目录创建失败\n' >&2
        [ "$status" -ne 0 ] || status=5
      elif ! cp -a "$SANDBOX_ROOT/runs/evidence/." \
        "$EVIDENCE_DIR/benchmark-artifacts/" 2>/dev/null; then
        printf 'acceptance.sh: benchmark 原始日志归档失败\n' >&2
        [ "$status" -ne 0 ] || status=5
      fi
    fi
  fi
  if [ -f "$WORK_PARENT/.g15-acceptance-owned" ]; then
    if ! rm -rf -- "$WORK_PARENT"; then
      printf 'acceptance.sh: 外部临时目录清理失败: %s\n' "$WORK_PARENT" >&2
      [ "$status" -ne 0 ] || status=5
    fi
  fi
  {
    printf 'finished_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'overall_exit_code=%s\n' "$status"
  } >> "$EVIDENCE_DIR/metadata.txt"
  printf 'acceptance.sh: exit=%s evidence=%s\n' "$status" "$EVIDENCE_DIR"
  trap - EXIT
  exit "$status"
}
trap finalize EXIT

{
  printf 'issue=144\n'
  printf 'entrypoint=bash scripts/acceptance.sh\n'
  printf 'source_sha=%s\n' "$SOURCE_SHA"
  printf 'source_tree=%s\n' "$SOURCE_TREE"
  printf 'source_short_sha=%s\n' "$SHORT_SHA"
  printf 'source_worktree_dirty=%s\n' "$SOURCE_WORKTREE_DIRTY"
  printf 'formal_evidence_eligible=%s\n' "$FORMAL_EVIDENCE_ELIGIBLE"
  printf 'source_snapshot_mode=%s\n' "$SOURCE_SNAPSHOT_MODE"
  printf 'started_utc=%s\n' "$STARTED_UTC"
  printf 'source_repository_name=%s\n' "$(basename "$REPO_ROOT")"
  printf 'temporary_copy=external_mktemp\n'
  printf 'runtime_tmp=external_mktemp\n'
} > "$EVIDENCE_DIR/metadata.txt"
printf '[acceptance] evidence=%s\n' "$EVIDENCE_DIR"

# A clean formal run reads the immutable commit object, not the mutable
# worktree. Dirty development runs are opt-in and are always marked ineligible.
if [ "$SOURCE_WORKTREE_DIRTY" = "false" ]; then
  if ! git -C "$REPO_ROOT" archive --format=tar "$SOURCE_SHA" \
    | tar -xf - -C "$SANDBOX_ROOT"; then
    die "无法从固定提交创建受测快照" 5
  fi
else
  if ! (
    cd "$REPO_ROOT"
    git ls-files --cached --others --exclude-standard -z \
      | tar --null -T - -cf -
  ) | tar -xf - -C "$SANDBOX_ROOT"; then
    die "无法创建脏工作树开发快照" 5
  fi
fi

(
  cd "$SANDBOX_ROOT"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
) > "$EVIDENCE_DIR/snapshot-sha256.txt"
SNAPSHOT_MANIFEST_SHA="$(sha256sum "$EVIDENCE_DIR/snapshot-sha256.txt" | awk '{print $1}')"
printf 'snapshot_manifest_sha256=%s\n' "$SNAPSHOT_MANIFEST_SHA" \
  >> "$EVIDENCE_DIR/metadata.txt"

export CONDA_EXE="$CONDA"
export PYTHONPATH="$SANDBOX_ROOT/src"
export TMPDIR="$RUNTIME_TMP"
export TMP="$RUNTIME_TMP"
export TEMP="$RUNTIME_TMP"
export PYTHONDONTWRITEBYTECODE=1
export BLACK_CACHE_DIR="$RUNTIME_TMP/black-cache"
# A single formatter worker is deterministic and avoids MSYS/Windows spawn
# deadlocks; Linux acceptance checks the same formatting result.
export BLACK_NUM_WORKERS="${BLACK_NUM_WORKERS:-1}"
export PYLINTHOME="$RUNTIME_TMP/pylint-cache"
export XDG_CACHE_HOME="$RUNTIME_TMP/xdg-cache"
unset G15_DENY_NETWORK G15_OFFLINE_GUARD_ACTIVE G15_OFFLINE_GUARD_VERBOSE

set +e
(
  set -e
  printf 'uname='; uname -a
  if [ -r /etc/os-release ]; then
    printf '%s\n' '--- /etc/os-release ---'
    cat /etc/os-release
  fi
  printf '%s\n' '--- tool versions ---'
  "$CONDA" --version
  git --version
  "$CONDA" run --no-capture-output -n pp03 python -c \
    'import platform, numpy, yaml; print("python=" + platform.python_version()); print("numpy=" + numpy.__version__); print("pyyaml=" + yaml.__version__)'
  "$CONDA" run --no-capture-output -n pp03 python -m pip check
) > "$EVIDENCE_DIR/environment.txt" 2>&1
ENVIRONMENT_RC=$?
set -e
printf 'environment_capture_exit_code=%s\n' "$ENVIRONMENT_RC" \
  >> "$EVIDENCE_DIR/metadata.txt"
if [ "$ENVIRONMENT_RC" -ne 0 ]; then
  die "环境信息/依赖自检失败（原始退出 $ENVIRONMENT_RC）" 2
fi

printf 'step\tstarted_utc\tcommand\texit_code\ttee_exit_code\n' \
  > "$EVIDENCE_DIR/steps.tsv"

run_step() {
  local name="$1"
  shift
  local started command_rc tee_rc rendered
  local -a display_args pipeline_status
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  display_args=("$@")
  if [ "${display_args[0]}" = "$CONDA" ]; then
    display_args[0]=conda
  fi
  printf -v rendered '%q ' "${display_args[@]}"
  printf '[acceptance] step=%s command=%s\n' "$name" "$rendered"
  set +e
  (
    cd "$SANDBOX_ROOT"
    "$@"
  ) 2>&1 | tee "$EVIDENCE_DIR/${name}.log"
  pipeline_status=("${PIPESTATUS[@]}")
  command_rc=${pipeline_status[0]}
  tee_rc=${pipeline_status[1]}
  set -e
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$name" "$started" "$rendered" "$command_rc" "$tee_rc" \
    >> "$EVIDENCE_DIR/steps.tsv"
  if [ "$tee_rc" -ne 0 ]; then
    printf 'acceptance.sh: 无法写入步骤日志 %s（tee 退出 %s）\n' \
      "$name" "$tee_rc" >&2
    return 5
  fi
  if [ "$command_rc" -ne 0 ]; then
    printf 'acceptance.sh: 步骤 %s 失败（退出 %s）\n' \
      "$name" "$command_rc" >&2
    return 4
  fi
}

run_step verify bash scripts/verify.sh
run_step fault_injection \
  "$CONDA" run --no-capture-output -n pp03 python -m pytest -q \
  tests/fault_injection/test_offline_operation.py \
  tests/fault_injection/test_pipeline_faults.py
run_step benchmark bash scripts/benchmark.sh

printf '[acceptance] all steps passed; evidence=%s\n' "$EVIDENCE_DIR"
