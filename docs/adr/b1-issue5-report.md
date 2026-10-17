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

| 口径 | 点数 | 分片 | 耗时 | 判定 |
| --- | --- | --- | --- | --- |
| 内核（趋势 + 噪声，**不含序列化与 Manifest**） | 1,000,000 | chunk_points 102,400 | **0.057 s** | PASS |
| 内核 + `np.savez` 10 片写盘（32,010,200 字节） | 1,000,000 | 10 片 | **0.073 s** | PASS |

两次都记录了 os / cpu / cpu_count / python / numpy / points / `scalar_values=2,000,000` / chunk_points / 计时范围。

> ⚠️ **这不是正式验收数字。** `docs/interface.md` §7.3 的正式口径要求计时覆盖「生成 + NPZ 分片写盘 + **完整 Manifest 落盘**」。Manifest 与原子写入器属 C4 的 `pipeline/serialization.py`、`producer.py`（#3、#11），**本 Issue 尚不具备**。第二行用的是 `np.savez`，**不是**项目的原子写入器，也**没有** Manifest 与 SHA-256 校验。正式测量须等 C4 落地后在 Ubuntu 24.04 与 RHEL 9 上重跑。
>
> 另：百万点口径按 `docs/interface.md` 暂按 `seq` 采样次数计点，同时记录 `sample_count=1,000,000` 与 `scalar_value_count=2,000,000`（双信号），**不把两个数合并宣称两个已验证采样点**。

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

1. **正式百万点口径未验证**（最高风险）。本 PR 只有内核与近似写盘数字，**未**覆盖完整 Manifest 落盘，也**未**在 Linux 上跑过。须由 C4 + C5 在双系统上产出正式数字。
2. **`SimulationChunk` 归属待定**。现定义在 `engine.py`，建议迁往 `src/g15/common/types.py`（属 A 白名单，B1 未自行迁移）。
3. **`rng` 参数保留但不使用**。为不改 §6 冻结签名而保留，实际随机性由 `(seed, 流名, 样本序号)` 决定。
4. **负值不钳位**。若 A 或 B2 要求钳位，属契约变更，需先评审。
5. **ADR 仍是草稿**，确定性策略需 A 评审后方可作为冻结依据。

### 依赖

- #3（分片契约，C4）—— 已按 `docs/interface.md` §5.1 实现输出，未实际联调
- #4（配置对象，C3）—— 未实现，本 Issue 用结构化读取绕开
- #11（producer，C4）—— 正式基准需要它

### 需要 A 决策的 4 项

1. `SimulationChunk` 是否迁往 `src/g15/common/types.py`
2. `simulate_chunk` 的 `rng` 参数去留
3. 负值策略（钳位 or 只报错）
4. 正式百万点计时口径（等 C4 的 `produce_run`）

---

## 七、下一步

1. A 已在 PR #15 提出 `CHANGES_REQUESTED`（2026-10-16T06:41:12Z），两条恒真断言已按上述修正并重跑门禁，请求复审。
2. A 已明确：本 PR 点名的性能测试与两份 ADR/报告可保留；ADR-0001 留给仿真草稿，Issue #2 用 0002–0004，ADR 仍待评审。
3. 共享类型与 RNG 行为由 **#17**（`refactor(contracts): 评审共享类型与 RNG 契约后迁移 SimulationChunk`，负责人 A，协作 B1/C4/C3/C1）跟踪。**本 PR 不擅自迁移 `common/types.py`**；`rng` 可选且不使用与原签名/行为的差异，待 #17 同步接口记录。
4. 合并后配合 C1（#6 故障注入）联测事件跨分片与重叠策略。
5. 配合 C4（#11 producer）与 C5 做含落盘的正式百万点基准，在 Ubuntu 24.04 + RHEL 9 各跑一次并留证。正式百万点须等完整生产入口及双 Linux 实测。

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
