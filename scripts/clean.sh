#!/usr/bin/env bash
# clean.sh —— 只清理明确的自动产物，绝不删除源码、文档、配置或用户数据
# （docs/development.md：clean.sh 不能随意删除源码/用户数据）。
#
# 清理范围（全部为可自动重建的产物）：
#   runs/                     运行产物（NPZ 分片、SQLite、报告、证据日志）
#   .pytest_cache/            pytest 缓存
#   **/__pycache__/  *.pyc    Python 字节码缓存
#   .coverage                 覆盖率数据
#   src/*.egg-info/           可编辑安装的构建元数据
#
# 退出码：0 成功；2 前置条件不满足；3 清理执行失败。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "[clean] 仓库根: $REPO_ROOT"

# ---- runs/：整体运行产物目录（.gitignore 已忽略）----
if [ -d runs ]; then
  rm -rf runs
  echo "[clean] 已删除 runs/（运行产物）"
else
  echo "[clean] runs/ 不存在，跳过"
fi

# ---- 测试/字节码缓存 ----
rm -rf .pytest_cache
find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find . -type f -name '*.pyc' -delete 2>/dev/null || true
rm -f .coverage
echo "[clean] 已删除 pytest 缓存 / __pycache__ / .coverage"

# ---- 可编辑安装元数据（setup.sh 重跑即可重建）----
find src -maxdepth 1 -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true

echo "[clean] 完成。源码、文档、配置与 git 管理的文件均未触碰。"
