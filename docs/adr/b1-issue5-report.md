# B1 任务报告 — Issue #5 仿真内核（退化趋势 + 噪声）

| 项目 | 内容 |
| --- | --- |
| 角色 | B1 仿真器技术负责人 |
| Issue | [#5 仿真内核（退化趋势 + 噪声）](https://github.com/Zhu-ShuH/g15-predictive-maintenance/issues/5) |
| 分支 | `feat/5-simulation-kernel` |
| PR | [#15](https://github.com/Zhu-ShuH/g15-predictive-maintenance/pull/15) |
| 提交 | `9ba64e4 feat(sim): 实现仿真内核（退化趋势 + 噪声）(#5)` |
| 报告日期 | 2026-10-16 |
| 状态 | 已提交、已开 PR、CI 全绿；**待人工 Review 后合并** |

---

## 一、我做了什么（操作清单）

### 1. 前期调研（只读，未改任何文件）

1. 读 `members/B1/AGENT.md`（96 行）——确认角色白名单、禁改清单、验收标准 4 条、会话报告格式 5 项。
2. 读 `docs/interface.md`（460 行）——重点 §4 配置契约、§5.1 分片契约、§6 冻结接口签名、§7.1 仿真模型、§7.3 性能口径、§14 需求追踪（R03 同 seed 可复现、R04 百万点 <60 s）。
3. 读 `docs/development.md`、`docs/roadmap.md`、`docs/onboarding.md`、`docs/ai_prompts.md`、`00_先读_统一决策与待确认事项.md`、`AGENTS.md`、`AGENTS.local.md`。
4. 读 `gh issue view 5` 正文，抄下修改范围原文。
5. 摸清仓库现状：`src/g15/` 下 28 个 `.py` 全部 0 字节；`configs/*.yaml` 7 个全是占位注释；`tests/unit/` 只有 `.gitkeep`。
6. 查本机参考目录 `py02`（课程实训项目）—— 只有课程作业，**与 G15 无关，无可复用代码**。
7. 读教师任务书 `任务清单.pdf`（用 pypdf 提取 10 页文本，定位到 G15 条目）与 `完成标准.txt`。

### 2. 建分支

```
git switch -c feat/5-simulation-kernel   # 从 main (75037f4)
```

### 3. 写代码（8 个文件，全部在白名单内）

### 4. 跑门禁、修问题

迭代修了 6 类问题（详见第四节）。

### 5. 跑百万点基准，取真实数字

> **2026-10-20 补充**：`#11` 合入后，已用真实 `produce_run_detailed` 补出完整生产口径的三次连测（中位数 0.1551 s）。明细见 §三第 2 小节。

### 6. 提交、推送、开 PR

```
git add <8 files>
git commit -m "feat(sim): 实现仿真内核（退化趋势 + 噪声）(#5)"   # 9ba64e4
git push -u origin feat/5-simulation-kernel
gh pr create --base main --head feat/5-simulation-kernel
```

### 7. 处理 GitHub 通知

- **PR #14 review requested** → 逐条核实文档与 Issue 正文，发了 review 意见。
- **Issue #5 assigned** → 发了进展报告评论。

---

## 二、变更文件（精确路径）

| # | 文件 | 增/删 | 说明 |
| --- | --- | --- | --- |
| 1 | `src/g15/simulation/degradation.py` | +176 / -0 | 向量化趋势 + 参数校验 |
| 2 | `src/g15/simulation/noise.py` | +227 / -0 | 块寻址噪声 |
| 3 | `src/g15/simulation/engine.py` | +538 / -0 | `simulate_chunk` 主入口 + 类型 |
| 4 | `tests/unit/test_simulation_engine.py` | +599 / -0 | 43 个用例 |
| 5 | `tests/unit/test_simulation_degradation.py` | +145 / -0 | 15 个用例 |
| 6 | `tests/unit/test_simulation_noise.py` | +183 / -0 | 18 个用例 |
| 7 | `tests/performance/test_simulation_benchmark.py` | +252 / -0 | 百万点基准（默认跳过） |
| 8 | `docs/adr/0001-simulation-model-and-determinism.md` | +135 / -0 | ADR 草稿，待 A 评审 |

**合计 8 文件 +2255 / -0，全部为新增，未修改任何既有文件。**

**白名单自检**：逐条比对 `members/B1/AGENT.md` 与 Issue #5 的修改范围，8 个文件全部落在范围内，**无一越界**。

**未改动**（禁改清单全部未碰）：`src/g15/config.py`、`src/g15/simulation/faults.py`、`src/g15/pipeline/*`、`src/g15/analysis/*`、`src/g15/cli.py`、`configs/*.yaml`、`docs/` 下非 ADR 文件、`.github/*`。

---

## 三、测试证据（真实命令 + 退出码）

平台：**Windows 11 (10.0.26100)**，**Python 3.12.15**，**NumPy 2.5.4**，解释器 `.venv/Scripts/python.exe`。

| 命令 | 退出码 | 关键结果 |
| --- | --- | --- |
| `python -m black --check src tests` | **0** | `34 files would be left unchanged` |
| `python -m pylint src/g15 --fail-under=8.5` | **0** | `9.97/10` |
| `python -m pytest -q` | **0** | `82 passed, 6 skipped in 0.21s` |
| `python -m pytest -q tests/unit/test_simulation*.py` | **0** | `81 passed in 0.17s`（Issue #5 验收命令） |
| `python -c "import g15"` | **0** | 无输出 |
| `G15_RUN_BENCHMARK=1 python -m pytest -q -s tests/performance/test_simulation_benchmark.py` | **0** | `6 passed in 0.72s` |

CI（GitHub Actions，push + pull_request 两次触发）：`skeleton (3.11)` 与 `skeleton (3.12)` **全部 pass**。

### 百万点基准实测

#### 1. 本 Issue 交付时的口径（历史记录，保留原样）

| 口径 | 点数 | 分片 | 耗时 | 判定 |
| --- | --- | --- | --- | --- |
| 内核（趋势 + 噪声，**不含序列化与 Manifest**） | 1,000,000 | chunk_points 102,400 | **0.057 s** | PASS |
| 内核 + `np.savez` 10 片写盘（32,010,200 字节） | 1,000,000 | 10 片 | **0.073 s** | PASS |

两次都记录了 os / cpu / cpu_count / python / numpy / points / `scalar_values=2,000,000` / chunk_points / 计时范围。

> ⚠️ **当时这不是正式验收数字。** `docs/interface.md` §7.3 的正式口径要求计时覆盖「生成 + NPZ 分片写盘 + **完整 Manifest 落盘**」。Manifest 与原子写入器属 C4 的 `pipeline/serialization.py`、`producer.py`（#3、#11），**当时本 Issue 不具备**。第二行用的是 `np.savez`，**不是**项目的原子写入器，也**没有** Manifest 与 SHA-256 校验。
>
> 另：百万点口径按 `docs/interface.md` 暂按 `seq` 采样次数计点，同时记录 `sample_count=1,000,000` 与 `scalar_value_count=2,000,000`（双信号），**不把两个数合并宣称两个已验证采样点**。

#### 2. 完整生产口径补测（2026-10-20 / 10-21，`#11` 合入后）

`#11`（C4 `produce_run`）已合入 main（`614b256`），上面缺的那一段现在可以补测。用**真实生产入口**、**真实配置**、**官方计时窗口**连跑 3 次：

- 配置：`configs/benchmark_1m.yaml`（`scenario_id=benchmark_1m`、`total_points=1,000,000`、`chunk_points=102,400`、`sample_rate_hz=100`、`seed=42`）
- 入口：`g15.pipeline.producer.produce_run_detailed(config, output_root)`
- 计时：`producer.py` 内 `time.perf_counter()` 窗口，覆盖随机初始化 → 趋势/噪声生成 → 序列化 → 文件写入 → SHA-256 → 最后 complete Manifest 原子发布，与 `docs/acceptance.md` §2 逐条吻合
- 每次跑在**独立空目录**（`run1`/`run2`/`run3`），互不复用
- 环境：Windows 11（10.0.26100）/ Python 3.12.15 / NumPy 2.5.4

**A 轮（2026-10-20，head `8561014` 首次测）**

| 次数 | `elapsed_seconds`（官方窗口） | 分片 | 点数 | 标量值 |
| --- | --- | --- | --- | --- |
| 第 1 次 | **0.1558 s** | 10 | 1,000,000 | 2,000,000 |
| 第 2 次 | **0.1530 s** | 10 | 1,000,000 | 2,000,000 |
| 第 3 次 | **0.1551 s** | 10 | 1,000,000 | 2,000,000 |

- 中位数 **0.1551 s**，最大值 **0.1558 s**
- Manifest 实测：`status=complete`、`parts=10`、`rows` 之和 `1,000,000`，分片形状 `[102400 × 9, 78400]`，与 `docs/interface.md` §5.1 的短尾片约定一致
- 产物结构：`raw/part-000000.npz` ~ `part-000009.npz` + `manifest.json` + `logs/app.jsonl`

> 这一轮的完整源码 SHA、模块来源、配置哈希与逐次原始输出**当时未留证**，只有汇总数字。A 在 PR #53 的审查（`2026-10-20T17:09:13Z`）要求补全，因此补了下面的「B 轮」。

**B 轮（2026-10-21，带完整可追溯记录重测）**

被测版本与调用方式（全部可复核）：

| 项 | 值 |
| --- | --- |
| `git rev-parse HEAD` | `8561014c2fecd14ce065eb52de77f083f9c13cc2`（= PR #53 head） |
| 分支 | `docs/5-full-production-benchmark` |
| `origin/main` | `7cfe71dec399054bd49d4edebb1c1881846b6b34` |
| `src/` `tests/` `configs/` 工作树改动 | **无**（`git status --porcelain -- src tests configs` 空） |
| `producer.py` SHA-256 | `4402bd2e75f1f4d1f42e735e1fe4d4fbb10665ca9771c1c8984a6e7e0e75a53e` |
| `engine.py` SHA-256 | `c2c66c33063d109369b32c5f99a5ef45e0f98c075631f1c7a1f46670fb037991` |
| `configs/benchmark_1m.yaml` SHA-256 | `47ceeee975bae5ef0a47dddcb00b4468fb74430fcd6e5cee5a906640e0b4c1ab`（685 B） |
| 实际加载的模块 | `g15` 包来自本工作树 `src/`（`sys.path.insert(0, "<repo>/src")`），非 site-packages |
| CPU / 逻辑核心 | Intel64 Family 6 Model 198 Stepping 2 / **20** |
| OS / Python / NumPy | Windows 11 10.0.26100 / 3.12.15 / 2.5.4 |

调用方式（完整可复制，见 PR #53 回复中的脚本）：`load_config("configs/benchmark_1m.yaml")` → 每组每次新建独立空目录 `mkdtemp()/g<组>r<次>`（`mkdir(exist_ok=False)`）→ `produce_run_detailed(cfg, dir)` → 读 `ProductionSummary.elapsed_seconds`。**两次运行是同一进程内的顺序调用，不是独立进程**，这一点如实写明。

两组各三次原始输出（`elapsed_seconds` 保留 Python float 全精度）：

| 组 | 次 | 开始 (UTC) | 结束 (UTC) | `elapsed_seconds` | 分片 | rows 之和 | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 2026-10-21T05:13:08Z | 2026-10-21T05:13:09Z | `0.1623431000000437` | 10 | 1,000,000 | complete |
| 1 | 2 | 2026-10-21T05:13:09Z | 2026-10-21T05:13:09Z | `0.22019989999989775` | 10 | 1,000,000 | complete |
| 1 | 3 | 2026-10-21T05:13:09Z | 2026-10-21T05:13:09Z | `0.26104460000010477` | 10 | 1,000,000 | complete |
| 2 | 1 | 2026-10-21T05:13:09Z | 2026-10-21T05:13:09Z | `0.16782149999994544` | 10 | 1,000,000 | complete |
| 2 | 2 | 2026-10-21T05:13:09Z | 2026-10-21T05:13:09Z | `0.1784910999999738` | 10 | 1,000,000 | complete |
| 2 | 3 | 2026-10-21T05:13:09Z | 2026-10-21T05:13:09Z | `0.16414430000008906` | 10 | 1,000,000 | complete |

- 第 1 组：中位数 **0.2202 s**，最大值 **0.2610 s**
- 第 2 组：中位数 **0.1678 s**，最大值 **0.1785 s**
- 合并 6 次：中位数 **0.1732 s**，最大值 **0.2610 s**，**全部严格 < 60 s**
- 测量值精度：`perf_counter()` 在 Windows 上分辨率约 100 ns 量级，表中保留 `repr(float)` 全精度，读数不确定度远小于 1 ms，不影响 <60 s 判定

**产物独立校验（事后重新读取，非生成时计算）**

每次运行后都重新打开每个 NPZ 独立核验，`sha_dtype_ok`、`seq_cont`、`rows_ok` 六次全为 `True`：

- 逐片重新计算 SHA-256 与 `manifest.parts[i].sha256` 比对 → 10/10 一致
- `read_chunk()` 重新读回，四数组 dtype = `int64 / int64 / float64 / float64`
- 每片 `len(seq) == entry.rows`；全部 10 片 `seq` 首尾相接且 `np.diff(seq) == 1`
- `rows` 之和 = `1,000,000` = `manifest.total_points`
- 每次 `manifest.json` 的 SHA-256 各不相同（如 `efbec0c7…` / `91751e6d…` / `57ca59cd…`），符合 `run_id` 每次不同

> **校验耗时未计入生产计时。** 上述 SHA 重算与 `read_chunk` 复核发生在 `produce_run_detailed` 返回**之后**，不在 `perf_counter()` 窗口内，因此上表数字纯为生产口径，未混入校验成本。

> ⚠️ **这仍然不是双 Linux 验收数字。** 上表是 **Windows 本机**、**单系统**、**C4 生产入口 + B1 内核**的联测结果，证明完整生产口径能在远低于 60 秒内完成，但 `docs/acceptance.md` §2 要求的是 Ubuntu 24.04 与 RHEL 9 **每个系统各自**在独立空运行目录连续测 3 次并留证。**Linux 侧数字仍待 C6（`#10`）与 C5（`#9`）产出**，本表不能替代。
>
> **峰值内存**：本报告**未测**。A 在 #51 已裁定口径（M2）：由独立进程从外部测峰值 RSS，**不扩 `ProductionSummary`**。因此「未测」的理由是「本轮未做外部进程测量」，不是「`ProductionSummary` 没有字段」。具体实现由 `#51` 跟踪。

**C 轮（2026-10-21，逐次留证，回应 A 在 `5599100429` 的四条要求）**

被测身份（本轮重新核对，代码 blob 与 B 轮完全一致，差异只在报告文档）：

| 项 | 值 |
| --- | --- |
| `git rev-parse HEAD` | `604bc99b7da4bb6327f88e068342ef51a7fde17f` |
| 与 B 轮的代码差异 | `git diff --stat 8561014..604bc99` 只有 `docs/adr/b1-issue5-report.md`；`producer.py` / `engine.py` / `configs/benchmark_1m.yaml` 的 git blob 三个全 **SAME** |
| `producer.py` SHA-256 | `4402bd2e75f1f4d1f42e735e1fe4d4fbb10665ca9771c1c8984a6e7e0e75a53e` |
| `engine.py` SHA-256 | `c2c66c33063d109369b32c5f99a5ef45e0f98c075631f1c7a1f46670fb037991` |
| `configs/benchmark_1m.yaml` SHA-256 | `47ceeee975bae5ef0a47dddcb00b4468fb74430fcd6e5cee5a906640e0b4c1ab` |
| 实际加载的模块 | `g15` 来自本工作树 `src/`（脚本打印 `g15.__file__` = `<worktree>/src/g15/__init__.py`） |
| OS / Python / NumPy | Windows-11-10.0.26100-SP0 / 3.12.15 / 2.5.4 |
| CPU / 逻辑核心 | Intel64 Family 6 Model 198 Stepping 2 / **20** |

**完整可复制的测量与独立校验脚本**（本轮实际执行的那一份，未做任何删改；`REPO` 由脚本自身位置推出，`sys.path.insert(0, REPO/"src")` 保证加载的是被测树而非 site-packages）：

```python
"""#53 B 轮完整测量 + 事后独立校验脚本（可复制执行）。

运行：PYTHONPATH=<worktree>/src python .tmp-measure.py
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "src"))

from g15.config import load_config                       # noqa: E402
from g15.pipeline import producer                        # noqa: E402
from g15.pipeline.serialization import read_chunk        # noqa: E402

CONFIG = REPO / "configs" / "benchmark_1m.yaml"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def verify(run_dir: Path) -> dict:
    """事后独立校验：重算每片 SHA-256、重读 NPZ、检查 seq 连续与 rows 之和。"""
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    shard_sha_ok = 0
    seq_contiguous = True
    dtypes_ok = True
    rows_match = True
    previous_last_seq = None
    rows_sum = 0

    for entry in manifest["parts"]:
        part_path = run_dir / entry["path"]
        if sha256_of(part_path) == entry["sha256"]:
            shard_sha_ok += 1
        chunk = read_chunk(part_path, type("E", (), entry)())
        if (chunk.seq.dtype != np.int64 or chunk.timestamp_ns.dtype != np.int64
                or chunk.vibration.dtype != np.float64
                or chunk.temperature.dtype != np.float64):
            dtypes_ok = False
        if len(chunk.seq) != entry["rows"]:
            rows_match = False
        if previous_last_seq is not None and int(chunk.seq[0]) != previous_last_seq + 1:
            seq_contiguous = False
        if len(chunk.seq) > 1 and not bool(np.all(np.diff(chunk.seq) == 1)):
            seq_contiguous = False
        previous_last_seq = int(chunk.seq[-1])
        rows_sum += len(chunk.seq)

    return {
        "manifest_sha256": sha256_of(manifest_path),
        "parts": len(manifest["parts"]),
        "rows_sum": rows_sum,
        "status": manifest["status"],
        "shard_sha_ok": f"{shard_sha_ok}/{len(manifest['parts'])}",
        "seq_contiguous": seq_contiguous,
        "dtypes_ok": dtypes_ok,
        "rows_match": rows_match,
    }


def main() -> None:
    print(json.dumps({
        "kind": "environment",
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "cpu_count": __import__("os").cpu_count(),
        "g15_module": __import__("g15").__file__,
        "producer_module": producer.__file__,
        "config_path": str(CONFIG),
        "config_sha256": sha256_of(CONFIG),
        "producer_sha256": sha256_of(Path(producer.__file__)),
        "engine_sha256": sha256_of(Path(__import__("g15.simulation.engine", fromlist=["x"]).__file__)),
    }, ensure_ascii=False))

    config = load_config(CONFIG)
    for group in (1, 2):
        for index in (1, 2, 3):
            label = f"g{group}r{index}"
            root = Path(tempfile.mkdtemp(prefix=f"g15-{label}-"))
            run_root = root / label
            run_root.mkdir(exist_ok=False)

            started_at = utc_now()
            t0 = time.perf_counter()
            summary = producer.produce_run_detailed(config, run_root)
            elapsed = time.perf_counter() - t0
            ended_at = utc_now()

            t1 = time.perf_counter()
            checked = verify(summary.run_dir)
            verify_elapsed = time.perf_counter() - t1

            record = {
                "kind": "run",
                "label": label,
                "group": group,
                "index": index,
                "start_utc": started_at,
                "end_utc": ended_at,
                "elapsed_seconds": repr(summary.elapsed_seconds),
                "parts": summary.part_count,
                "total_points": summary.total_points,
                "scalar_values": summary.scalar_value_count,
                "fault_events": summary.fault_events,
                "verify_elapsed_seconds": repr(verify_elapsed),
                "run_dir": str(summary.run_dir),
                **checked,
            }
            print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
```

**六次逐条原始摘要**（`elapsed_seconds` 与 `verify_elapsed_seconds` 均为 `repr(float)` 全精度；`verify` 列是**事后独立校验**的耗时，**不计入**生产计时窗口）：

| 标签 | 开始 (UTC) | 结束 (UTC) | `elapsed_seconds` | 分片 | rows 之和 | status | `manifest.json` SHA-256 | 逐片 SHA | seq 连续 | dtype | rows 匹配 | `verify` 耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `g1r1` | 2026-10-21T14:07:27Z | 2026-10-21T14:07:27Z | `0.23739449999993667` | 10 | 1,000,000 | complete | `be4228c86bbb4c6bf4bee7672e94304f424a7b9202ed27bcf1cd3420b8034b23` | 10/10 | True | True | True | `0.08640260000174749` |
| `g1r2` | 2026-10-21T14:07:27Z | 2026-10-21T14:07:27Z | `0.2753052000007301` | 10 | 1,000,000 | complete | `54fbcd07ec6fe69750c430d21c893abac8f8cf4665a95283a9904916ae5acd7e` | 10/10 | True | True | True | `0.08519320000050357` |
| `g1r3` | 2026-10-21T14:07:27Z | 2026-10-21T14:07:28Z | `0.20490909999716678` | 10 | 1,000,000 | complete | `059d426b6e341b214864b76f8fc9c3d11ba5d96f6131ebd2e65b0455f2b6493c` | 10/10 | True | True | True | `0.09271760000046925` |
| `g2r1` | 2026-10-21T14:07:28Z | 2026-10-21T14:07:28Z | `0.21457110000119428` | 10 | 1,000,000 | complete | `3e66de9e2e74f2965b9bf4f8738d88e2737b69161788a198f09e185a481bf808` | 10/10 | True | True | True | `0.08247069999924861` |
| `g2r2` | 2026-10-21T14:07:28Z | 2026-10-21T14:07:28Z | `0.22041830000307527` | 10 | 1,000,000 | complete | `735670c39745cad48c44096b8ac423f132c3506538909085965a13749139b7c4` | 10/10 | True | True | True | `0.08146820000183652` |
| `g2r3` | 2026-10-21T14:07:28Z | 2026-10-21T14:07:29Z | `0.20028409999940777` | 10 | 1,000,000 | complete | `c37f28fee512f6deb95f4caf9fab06dc2ae4c34f19a32b80b4f18f4492c7e3c8` | 10/10 | True | True | True | `0.0823646999997436` |

- 第 1 组：中位数 **0.2374 s**，最大值 **0.2753 s**
- 第 2 组：中位数 **0.2146 s**，最大值 **0.2204 s**
- 合并 6 次：中位数 **0.2175 s**，最大值 **0.2753 s**，**全部严格 < 60 s**
- 每次的 `manifest.json` SHA-256 各不相同（`run_id` 含时间戳与随机后缀），可作为逐次追溯锚点
- **两组仍是同一进程内的顺序调用，不是独立进程**；「独立空运行目录」指每次 `mkdtemp()/g<组>r<次>` 且 `mkdir(exist_ok=False)`，不等于独立进程隔离，这一点如实写明

> **A 轮 / B 轮 / C 轮的关系**：A 轮（0.1558/0.1530/0.1551 s）**当时未留逐次证据**，不能补成原始记录；B 轮（0.1623…0.2610 s）保留了耗时与产物校验结论，但**未保存逐次 Manifest 摘要**，同样无法事后补造。C 轮是唯一一轮「每次运行都有独立 Manifest 摘要与逐片校验结果」的记录，因此**以 C 轮为准**。三轮数字的差异属机器负载波动，不构成性能回归——三轮的代码 blob、配置、分片数与点数完全相同。

### 三项 `test_config.py` 失败的独立留证（与 PR #53 无关的既有环境差异）

`pytest -q` 全量运行时有 3 条用例在 `tests/unit/test_config.py` 失败。A 在 PR #53 审查中指出「当前证据不足以认定中文路径是根因」，**这个批评成立，先前的归因是错的**。下面给出实测证据与更正后的归因。

**命令与退出码（同一台机器，同一 Python 3.12.15 / NumPy 2.5.4）**

| 被测树 | `HEAD` | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- | --- |
| 固定 main | `7cfe71dec399054bd49d4edebb1c1881846b6b34` | `pytest tests/unit/test_config.py -q` | **1** | 3 failed, 90 passed |
| PR #53 head | `8561014c2fecd14ce065eb52de77f083f9c13cc2` | `pytest tests/unit/test_config.py -q` | **1** | 3 failed, 90 passed |

两棵树的 `src/`、`tests/`、`configs/` 工作树改动均为空（`git status --porcelain -- src tests configs` 无输出），失败集合逐条相同：

- `test_zero_window_points_is_rejected`
- `test_incomplete_window_policy_is_checked`
- `test_health_rules_path_is_independent_of_cwd`

**完整失败摘要（PR head 上取得）**

```
tests\unit\test_config.py:742: in test_zero_window_points_is_rejected
    expect_invalid(...)
tests\unit\test_config.py:85: in expect_invalid
    assert fragment in message, message
E   AssertionError: health_rules_file: file not found:
    C:\Users\hejiahui\PycharmProjects\<仓库目录>\health_rules.yaml
E   assert 'window_points' in 'health_rules_file: file not found: ...'

tests\unit\test_config.py:757: in test_incomplete_window_policy_is_checked
E   AssertionError: health_rules_file: file not found: ...\health_rules.yaml
E   assert 'incomplete_window' in 'health_rules_file: file not found: ...'

tests\unit\test_config.py:789: in test_health_rules_path_is_independent_of_cwd
    first = load_consumer_config(path).health_rules_path
src\g15\config.py:1100: in load_consumer_config
src\g15\config.py:1079: in _build_consumer_config
src\g15\config.py:1051: in _resolve_health_rules
E   g15.config.ConfigError: health_rules_file: file not found: ...\health_rules.yaml
```

三条是同一个原因：前两条本来要断言「`window_points` / `incomplete_window` 非法」，但配置在更早的 `_resolve_health_rules` 就抛了 `ConfigError`，断言片段自然对不上。

**根因：`_resolve_health_rules` 的解析基准是「最近的含 `pyproject.toml` 的祖先」，而本机 pytest 的 `tmp_path` 落在仓库内部**

`src/g15/config.py` 的解析链（`inspect.getsource` 实测）：

```python
resolved = _repository_root(config_path) / candidate
resolved = resolved.resolve()
if not resolved.is_file():
    raise ConfigError("health_rules_file", f"file not found: {resolved}")
```

`_repository_root` 自 `config_path.parent` 向上找 `pyproject.toml`，找不到才回退到文件自身目录。测试 `consumer_document()`（`tests/unit/test_config.py:61`）把 `health_rules.yaml` 写在 `tmp_path` 里、并把 `health_rules_file` 设为相对路径 `health_rules.yaml`，**期望相对配置文件目录解析**。

本机实测的解析基准。**下表所有「工作树根」都指同一个目录**：当时的工作树 `<worktree>`（即 `.tmp-m53`）。它包含 `pyproject.toml`，因此它既是工作树的根，也是 `_repository_root` 向上找到的返回值。初稿把同一目录分别写成「仓库根」与 `<仓库>\.tmp-m53`，读起来像两个不同的位置，这里统一成「工作树根」：

| 项 | 实测值 |
| --- | --- |
| `TEMP` / `TMP` 环境变量 | `C:\Users\hejiahui\AppData\Local\Temp` |
| 该目录当时可写？ | **否**，`PermissionError: [WinError 5] 拒绝访问` |
| `tempfile.gettempdir()` 实际返回 | **工作树根**（`TEMP` 不可写后回退到 cwd） |
| pytest `tmp_path` 实际位置 | `<工作树根>\pytest-of-hejiahui\...` |
| `_repository_root(config_path)` | `<工作树根>`（因为 `<工作树根>\pyproject.toml` 存在） |
| 测试写入规则文件的位置 | `<tmp_path>\health_rules.yaml` |
| 实现实际查找的位置 | `<工作树根>\health_rules.yaml`（不存在） |

**对照实验**：在**工作树根**临时放一个 `health_rules.yaml`（内容 `rules: []`），重跑同一命令 → **`93 passed`，退出码 0**；删除该文件后恢复 3 failed。即在这台机器上，失败的充分必要条件是「规则文件被解析到工作树根而那里没有该文件」。该工作树根的完整路径同样含中文，所以这个对照同时排除了「中文路径本身导致解析失败」的解释——把文件放到那个含中文的目录下就能通过。初稿把这一点写成「中文只是控制台按 GBK 渲染的乱码」，是对现象的解释而非对照实验直接测到的结论，这里只保留对照实验支持的部分。

**为什么 CI 不失败**：Linux CI 上 pytest 的 basetemp 在 `/tmp/pytest-of-*`（仓库外），祖先没有 `pyproject.toml`，`_repository_root` 回退到 `tmp_path` 自身，规则文件正好在那里，测试通过。本机因 `TEMP` 不可写而把 basetemp 退到仓库内，才触发。

**这条是否需要修**：`_resolve_health_rules` 当前把「相对规则文件」解析到仓库根而非配置文件目录，是**实现与测试期望不一致**，属 `src/g15/config.py`（C3 范围）与 `tests/unit/test_config.py` 的既有分歧，不在 B1 白名单内，本报告只留证、不改代码。建议由 C3 在 `#52` 之后单独确认契约（相对路径到底相对谁），再决定改实现还是改测试。

### 单元测试覆盖（对应 `members/B1/AGENT.md` 验收标准 4 条）

同 seed 逐点一致 / 不同 seed 噪声有别 / 分片与访问顺序无关 / dtype 与 shape / `seq` 连续 / 时间戳严格递增与低采样率精确 / 空块与零噪声 / 负点数与负索引拒绝 / 超 `total_points` 拒绝 / 最后不足块 / 非法噪声与负退化率拒绝 / 非有限 baseline 报错 / 溢出报错 / 缺字段报错路径 / 温度与振动双信号 / 噪声流相互独立 / `linear_trend` 纯函数 / `SimulationChunk.validate()` 捕获坏契约 / 两种配置布局。

---

## 四、技术要点与设计决策

### 4.1 核心难点：顺序 RNG vs 可切片管线

契约强制用 `numpy.random.default_rng(seed)`，而 `default_rng` 是**顺序生成器**——第 n 个样本的值取决于此前取过多少样本。但数据要切成 `chunk_points` 分片，且可能重试、续跑、改分片尺寸。若顺序取数，「同 seed 逐点一致」**只在一个分片尺寸下成立**。

**解决**：噪声按块随机寻址，不按抽取顺序。

- 噪声按 `BLOCK_POINTS = 16384` 分块
- 块索引 `b` 的熵 = `SeedSequence([seed & 0xFFFFFFFFFFFFFFFF, crc32(流名) & 0xFFFFFFFF, b])`
- 样本 `i` 只由 `(seed, 流名, i)` 决定
- 任意区间 `[start, start+count)` 只重算跨到的整块再切片，最多多算 2 块

效果：**分片大小、分片顺序、重试、断点续跑都不改变任何一点的数值**，且仍严格使用契约要求的 `default_rng`。

流标签用 `zlib.crc32` 而非内置 `hash()`——后者受 `PYTHONHASHSEED` 影响，跨进程不稳定。

### 4.2 时间戳用纯整数运算

```
whole_seconds = seq // rate
remainder     = seq % rate
offset        = remainder * 1_000_000_000 + rate // 2
offset       //= rate              # 四舍五入到最近纳秒
offset        += whole_seconds * 1_000_000_000
timestamp_ns  = offset + start_time_utc_ns
```

不用浮点累加，避免长跑漂移（§4 明确要求「不以每循环累加浮点时间」）。

### 4.3 物理合理性只查有限性，不钳位

NaN / Inf 抛出带**绝对样本序号**的 `ValueError`；**负值不钳位**。理由：振动与温度在物理上都可合理取负，静默裁剪等于伪造数据；真正的判据属 B2。此决策已写入 ADR 待 A 确认。

### 4.4 对未决契约的处理

`ScenarioConfig`（C3 的 `config.py`）与 `SimulationChunk` 的归属在仓库里都还不存在，而 `config.py` 明确禁止 B1 修改。因此：

- `engine.py` 用**结构化读取**接受任意具备所需字段的对象，同时支持嵌套（`scenario` / `signals`）与扁平两种布局
- `SimulationChunk` 定义在 `engine.py` 内
- **C3 的 `config.py` 未实现不阻塞本 Issue**；若 A 决定迁往 `common/types.py`，只需改一处 import

### 4.5 A 审查打回后的两条测试修正

A 在 PR #15 的 `CHANGES_REQUESTED` 里点名两条恒真断言。两条都改了，并各自证明「断言失效时测试真的会红」。

**修正 1：`rng` 未被消耗（原 `:153`）**

原写法 `assert generator.bit_generator.state["state"]["state"] == 7 or True` 有两个毛病：`or True` 让它永远成立；而且只比较一个计数器字段，即便状态被部分推进也可能碰巧相等。

改为在调用前后各深拷贝一份完整状态再比较：

```python
before = copy.deepcopy(generator.bit_generator.state)
with_rng = simulate_chunk(DEFAULT_SCENARIO, 0, 512, generator)
after = copy.deepcopy(generator.bit_generator.state)
assert after == before
```

**证明它可证伪**（独立探针）：拿一个 `default_rng(7)`，抽 1 个标准正态数后再比较状态 →

```
state_unchanged_after_one_draw = False
would_test_fail = True
```

即只要 `simulate_chunk` 动过生成器一次，这条断言就会失败。

**修正 2：负值不钳位（原 `:525`）**

原写法 `assert float(chunk.vibration[0]) < 0.0 or True` 靠「随机第一点是否小于零」来判断，既恒真又依赖随机性。

改为明确的负 baseline + 零噪声 + 已知退化率，断言手算结果：

```python
config = signals(vibration={
    "baseline": -2.0,
    "degradation_rate_per_sample": 0.5,
    "noise_std": 0.0,
})
chunk = simulate_chunk(config, 0, 4)
assert list(chunk.vibration) == [-2.0, -1.5, -1.0, -0.5]
assert bool(np.all(chunk.vibration < 0.0))
```

零噪声让结果完全可手算；若实现里有任何钳位（例如 `np.maximum(x, 0)`），四个值都会变成 0，断言立刻失败。

---

## 五、修过的问题（迭代记录）

| # | 问题 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 配置字段值为 `None` 时误判为「缺失」 | 哨兵用 `object()`，与 `None` 不可区分 | 改为 `_Absent` 类 + `_ABSENT` 单例 |
| 2 | 35 个测试连带失败 | `_nested()` 签名从 3 参改 2 参时漏改 2 处调用点 | 同步调用点 |
| 3 | `linear_trend` 溢出时同时喷 RuntimeWarning 和 raise | 溢出本由调用方统一报错 | 包 `np.errstate(over='ignore', invalid='ignore')` |
| 4 | `block_points=-8` 报错文案不符测试期望 | 用了「非负」校验而非「正数」校验 | 拆出 `_as_int`，改用「正数」校验 |
| 5 | 一个测试语义写错 | 把两通道字段拼进同一个扁平 dict，丢掉了通道区分 | 改为两个子 dict |
| 6 | `SyntaxError`，pytest 收集失败 | `edit` 删空行时把两行拼成了一行 | 拆回两行 |
| 7 | black 长时间无输出 | `.venv` 内 black 缓存损坏 | 清缓存后正常 |
| 8 | 两条断言永远成立（A 审查打回） | 写了 `assert ... or True`，等价于没断言 | 见下方「4.5」 |

> 教训记录：第 6 条是我自己的编辑事故——用 `edit` 改代码时 `old_string` / `new_string` 必须精确包含换行符。
> 第 8 条是 A 在 PR #15 审查中点名的：`tests/unit/test_simulation_engine.py:153` 与 `:525` 两处 `assert ... or True` 恒真，等于没有测试。已改为可证伪的断言，见 4.5。

---

## 六、风险 / 依赖 / 待审批

### 风险

1. **双 Linux 正式验收仍未做**（最高风险）。`#11` 已合入 main（`614b256`），本报告 §三「完整生产口径补测」已补出 Windows 本机三次连测数字（中位数 0.1551 s），但 `docs/acceptance.md` §2 要求的是 Ubuntu 24.04 与 RHEL 9 **各自**连测 3 次并留证，**Linux 侧数字仍缺**，须由 C6（`#10`）与 C5（`#9`）产出。峰值内存亦未测（本轮未做外部进程测量，**不是** `ProductionSummary` 缺字段；口径由 `#51` 跟踪）。
2. **`SimulationChunk` 归属已定**。`#17` 合并为 `e8e2924`，唯一公共定义在 `src/g15/common/types.py`，`engine.py` 保留同名再导出（本条原为待定，现为已决）。
3. **`rng` 参数保留但不使用**。为不改 §6 冻结签名而保留，实际随机性由 `(seed, 流名, 样本序号)` 决定；`docs/interface.md` §15 已记录不得消费。
4. **负值不钳位**。若 A 或 B2 要求钳位，属契约变更，需先评审。
5. **ADR 仍是草稿**，确定性策略需 A 评审后方可作为冻结依据。

### 依赖

- #3（分片契约，C4）—— **已合入**（`e972836` 前），本 Issue 输出按 §5.1 形状，实测 `[102400 × 9, 78400]` 通过
- #4（配置对象，C3）—— **已合入**（`8d29cd3`），本 Issue 仍用结构化读取，两者兼容
- #11（producer，C4）—— **已合入**（`614b256`），§三第 2 小节的三次连测即用它产出

### 需要 A 决策的 4 项（现状）

1. `SimulationChunk` 是否迁往 `src/g15/common/types.py` —— **已定稿**（`#17`/`e8e2924`，已迁）
2. `simulate_chunk` 的 `rng` 参数去留 —— **已定稿**（保留、不得消费）
3. 负值策略（钳位 or 只报错）—— **仍未决**
4. 正式百万点计时口径 —— **口径已定**（`docs/acceptance.md` §2：每系统 3 次），**Linux 侧数字待 C6/C5**

---

## 七、下一步

> **2026-10-19 状态回填**（本报告写于 PR #15 评审期，以下条目保留原文，结果就地补在每条后面，便于对照「当时计划」与「实际发生」）。

1. ~~A 已在 PR #15 提出 `CHANGES_REQUESTED`（2026-10-16T06:41:12Z），两条恒真断言已按上述修正并重跑门禁，请求复审。~~ **已完成**：A 复审通过，PR #15 于 2026-10-17T09:23:46Z 合并为 `2c14759`，Issue #5 关闭。
2. A 已明确：本 PR 点名的性能测试与两份 ADR/报告可保留；ADR-0001 留给仿真草稿，Issue #2 用 0002–0004，ADR 仍待评审。**现状**：上述文件均已在 main 上；ADR-0001 仍是草稿状态，本 PR 只回填已定稿的未决项，不改其「待 A 评审」属性。
3. 共享类型与 RNG 行为由 **#17**（`refactor(contracts): 评审共享类型与 RNG 契约后迁移 SimulationChunk`，负责人 A，协作 B1/C4/C3/C1）跟踪。**本 PR 不擅自迁移 `common/types.py`**；`rng` 可选且不使用与原签名/行为的差异，待 #17 同步接口记录。**已定稿**：#17 合并为 `e8e2924`，`SimulationChunk` 唯一公共定义在 `src/g15/common/types.py`，`engine.py` 保留同名再导出；`rng` 保留可选、明确不得消费，`docs/interface.md` §15 已记录。
4. 合并后配合 C1（#6 故障注入）联测事件跨分片与重叠策略。**已发生**：`#6` 合并为 `1f93018`，`simulate_chunk` 与其跨分片一致性实测通过。
5. 配合 C4（#11 producer）与 C5 做含落盘的正式百万点基准。按 `docs/acceptance.md` §2，Ubuntu 24.04 与 RHEL 9 **每个系统都要在独立空运行目录连续测 3 次、保留全部结果，3 次均须严格 < 60 秒，并给出中位数、最大值与峰值内存（未测内存则写未测）**；不得用一次运行或回填降低验收次数。**已部分完成**：`#11` 已合入（`614b256`），Windows 本机三次连测已补入 §三第 2 小节（中位数 0.1551 s、最大值 0.1558 s，全部 < 60 s）；**Linux 双系统三次连测仍待 C6（`#10`）与 C5（`#9`）**。

**建议 PR 标题**：`feat(sim): 实现仿真内核（退化趋势 + 噪声）(#5)`（已使用）

---

## 八、附：GitHub 通知处理记录

用户提供的通知截图中有两条未读：

1. **`Zhu-ShuH/g15-predictive-maintenance #14`** — `docs(guides): 新增全程开发指南、组员操作手册与 AI 提示词手册 (#12)` — **review requested** — 13 hours ago
   → 已逐条核实并提交 review。发现的问题包括：Issue #5 的修改范围被漏抄（`docs/ai_prompts.md:82/86`、`docs/roadmap.md:66` 只列 3 个 src 文件，漏掉 Issue 正文已授权的 `tests/unit/test_simulation*.py`，并称需另行申请）；同类错误批量出现在 #4、#6、#7、#8，其中 #7、#8 建议的文件名与 Issue 授权的 glob 不匹配（照做即越权）；`docs/onboarding.md:339` 称 Issue #12「尚不存在」但实际存在且 OPEN，与同文件 `:252` 冲突。已确认无误的部分包括路径、命令、`docs/interface.md` 小节号、行数、job 名等。

2. **`Zhu-ShuH/g15-predictive-maintenance #5`** — `仿真内核（退化趋势 + 噪声）` — **assigned** — 15 hours ago
   → 已在 Issue #5 发布进展报告评论，含交付清单、本地证据、基准数字与 4 项待 A 决策事项。

---

*本报告由 B1 角色按 `members/B1/AGENT.md` 的会话报告格式 5 项组织。所有数字均来自本机实测，未运行的项已明确标注。*
