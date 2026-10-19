# ADR 0001：仿真内核的模型与确定性策略

- 状态：**草稿，待 A 评审**（`members/B1/AGENT.md` 规定：`docs/adr/` 的仿真模型讨论稿仅可草拟 PR，不直接合并）
- 负责人：B1
- 相关 Issue：#5（仿真内核：退化趋势 + 噪声）
- 相关契约：`docs/interface.md` §4 配置契约、§5.1 分片 NPZ、§6 模块公开接口、§7.1 仿真模型规范、§7.3 性能评价
- 日期：2026-10-15

> 本文件是**实现方案说明与决策记录**，不是「已验收通过」的证明。文中所有性能数字都标注了测量范围与限制，未实测的内容一律写「未实测」。

## 1. 背景与问题

`docs/interface.md` §7.1 规定了退化模型：

```
vibration(i)   = v0 + kv * i + εv(i)
temperature(i) = t0 + kt * i + εt(i)
ε ~ N(0, σ²)
```

§4 又要求：

> 采用 `numpy.random.default_rng(seed)`；为不同设备和噪声/故障机制建立稳定的子随机流，避免算法修改导致无关随机数漂移。同配置、同 seed、同版本生成数值应逐点一致。

这里有一处**契约内部的张力**，必须在动手前解决：

1. `numpy.random.default_rng(seed)` 是**顺序**生成器。它的第 *n* 个样本取决于**此前已经取过多少个样本**。
2. 但数据要被切成 `chunk_points` 大小的分片写入 NPZ（§5.1），分片可能重试、可能断点续跑、`chunk_points` 也可能被改。
3. 如果噪声按「顺序取数」生成，那么「分片 [0, 102400) 单独生成」与「整段一次生成后切片」的**第 5 万号样本就不是同一个数**——同 seed 可复现性只在这一个分片尺寸、这一次执行顺序下成立。

`members/B1/AGENT.md` 第 3 条要求明确「不同分片大小/重试情况下的确定性策略」，正是冲着这个矛盾来的。

## 2. 决策

### 决策一：噪声按「块」随机寻址，而不是顺序取数

把样本索引空间切成固定大小的块（`BLOCK_POINTS = 16384`）。块 `b` 的随机数由

```python
SeedSequence([seed & 0xFFFFFFFFFFFFFFFF, crc32(stream_name) & 0xFFFFFFFF, b])
```

派生，再交给 `np.random.default_rng(...)`。

请求区间 `[start, start + count)` 时，只重算它跨到的整块，再切出需要的那一段。因此**样本 *i* 的噪声值只由 `(seed, 流名, i)` 决定**，与分片边界、分片顺序、重试次数、`chunk_points` 取值全部无关。

`start` 落在块中间时，最坏情况是重算首尾各一个整块（≤ 2 × 16384 个样本的额外开销）。相对 `chunk_points` 默认的 102400，这是常数级浪费，不随分片数增长。

**仍然严格使用 `numpy.random.default_rng`**，满足 §4 的字面要求；块寻址只改变「种子怎么来」，不改变「用什么生成器」。

### 决策二：各信号流用 CRC-32 名字标签互相隔离

`vibration` 与 `temperature` 各自占一条独立子流，标签是 `crc32(流名)`。这样：

- 改动振动噪声模型，**不会**让任何一个温度样本漂移（§4「避免算法修改导致无关随机数漂移」）。
- 将来 C1 的故障注入再加第三条流（`fault`），同样不影响已冻结的两条。

用 `zlib.crc32` 而不是内置 `hash()`：后者带进程随机化（PYTHONHASHSEED），跨进程、跨平台不稳定，直接违反可复现性。

### 决策三：时钟用整数运算，不做浮点累加

`timestamp_ns = start_time_utc_ns + (seq // rate) * 1e9 + round((seq % rate) * 1e9 / rate)`

全程 `int64` 整数运算，由**绝对采样序号**推出。§4 明确禁止「以每循环累加浮点时间」——那样在 100 万点处会积累可见漂移。把 `seq` 拆成整秒和余数，也避免了 `seq * 1e9` 在百万点规模上的溢出风险。

### 决策四：物理合理性由「有限性」把关，负值不强行截断

`simulate_chunk` 在返回前对两条信号做 `np.isfinite` 全量检查，发现 NaN/Inf 时抛出带**绝对样本序号**的 `ValueError`，指明是哪条信号、哪个索引。

**但不对负值做截断或钳位。** 理由：

- §7.1 要求「数值较大时须防止溢出、负值或不合理物理值，处理策略需文档声明」。这里声明的策略是**主动暴露而不是静默修正**。
- 振动/温度本身可能合理地取负（例如相对基线的偏差、或配置了负的 baseline）。真正「不合理」的判据属于 B2 的特征与评分模块，不属于仿真内核。
- 静默钳位会伪造数据，违反 `members/B1/AGENT.md` 第 5 条。

`linear_trend` 内部用 `np.errstate(over="ignore", invalid="ignore")` 包住，让溢出只产生**一个明确的异常**，而不是「一条 NumPy 警告 + 后面某个地方才炸」。

### 决策五：`rng` 参数保留但有意不使用

§6 冻结的签名是：

```python
simulate_chunk(config, start_seq, count, rng: np.random.Generator) -> SimulationChunk
```

实现**接受**该参数并做类型检查（传错类型抛 `TypeError`），但**不从中取数**——噪声只走决策一的块寻址。

这是有意的取舍：如果真从传入的 `rng` 顺序取数，就会退回决策一被否掉的那个方案。把参数留在签名里，是为了不改公共接口（§6 的签名改动需要 A 审批）；把「不用它」写进 docstring，是为了让 C4 在接入 `produce_run` 时不会误以为可以用它来控制噪声。

> **待 A 评审**：如果 A 认为应当从签名中移除 `rng`，这是公共接口变更，需要 A 批准并更新 `docs/interface.md` §6。B1 不自行改动。

## 3. 被否掉的备选方案

| 方案 | 为什么否掉 |
|---|---|
| 顺序 `default_rng(seed).standard_normal(n)` 一次生成，按分片切片 | 分片尺寸一变、或某个分片重试，数值就变。直接违反「同 seed 逐点一致」。 |
| `SeedSequence(seed).spawn(n_streams)` 给每条流一个子序列 | `spawn` 是顺序的，没有随机访问能力；仍然要求「从头开始按顺序取」。 |
| `bit_generator.advance(n)` 跳到第 *n* 个样本 | `advance` 对底层位生成器精确，但 `standard_normal` 走的是 ziggurat 拒绝采样，**跳过的样本数与实际消耗的随机字不成正比**，跳不到想要的位置。 |
| 令 `seed_eff = f(seed, 流名, start_seq)`，每个分片独立播种 | 能保证「分片大小无关」，但同一分片内仍是从头顺序取数；且 `f` 依赖 `start_seq` 意味着**同一物理样本在不同分片起点下取值不同**，比方案一更差。 |
| 用 `hash()` 做流标签 | 进程随机化，跨进程不稳定。 |

## 4. 对下游的影响

- **C4（producer / serialization，#3、#11）**：可以任意决定分片顺序、重试任意分片、甚至并发写分片，生成的数据都逐位一致。`chunk_points` 是纯性能参数，不影响数值。
- **C1（故障注入，#6）**：`noise.py` 导出的 `stream_entropy(seed, stream, block)` 与 `block_generator(...)` 可复用，给 `fault` 流做同样的块寻址，从而保证故障位置也跨分片稳定。**`faults.py` 不在 B1 白名单内，由 C1 实现。**
- **C5（独立验证，#9）**：验证「同 seed 两次运行逐点一致」时，不需要控制分片方式；换 `chunk_points` 重跑也必须一致，这本身就是一条可用的验收用例。
- **B2（分析，#7）**：`SimulationChunk` 的四条数组长度相同、dtype 固定，可直接喂给特征提取。

## 5. 未决事项（需 A / 相关角色确认）

> **2026-10-19 回填**：本 ADR 草拟时的 4 项未决，第 1、2 项已由 `#17`（`e8e2924`）定稿，第 3 项在 C4 的 `produce_run` 落地后已重测（数字见 `docs/adr/b1-issue5-report.md` §6），第 4 项仍待 A / B2 决定。以下保留原始条目并就地补结论，不改写当初的判断，便于后来者看到「当时为什么这么问」。

1. **`ScenarioConfig` 与 `SimulationChunk` 的类型定义位置** —— **已定稿（#17，`e8e2924`）**：
   - `SimulationChunk` 的唯一公共定义已迁到 `src/g15/common/types.py`；`engine.py` 保留同名再导出，实测 `engine.SimulationChunk is common.types.SimulationChunk` 为 `True`，`__module__` 为 `g15.common.types`，因此按旧路径导入的调用方与测试不受影响；
   - `ScenarioConfig` 仍**不由 B1 定义**——`engine.py` 通过「结构化读取」接受任意带 `scenario`/`signals` 字段的映射或对象，这一条不因 `#17` 改变；
   - 依赖方向按 `docs/interface.md` §2 记录为 `simulation -> common`、`pipeline -> common`。
2. **`rng` 参数的去留** —— **已定稿（#17，采纳 B1 的方案 (a)）**：保留 `rng: np.random.Generator | None = None`，仅为调用契约兼容；实现**不得消费**调用方生成器状态，`docs/interface.md` §6 / §15 已写明「不能声称实际使用了传入 RNG」。因此该参数既不会改变数值，也不会破坏分片无关性。
3. **百万点正式计时口径**：教师要求 100 万点 <60 秒，小组口径为「生成 + 落盘」（`docs/acceptance.md`）。本 ADR 的 §6 只提供**算法段**数字；含 `produce_run` + Manifest 落盘的正式数字见 `docs/adr/b1-issue5-report.md` §6，双 Linux 留证仍由 C6（`#10`）负责。
4. **负值策略**：**仍未决**，当前实现不钳位（只校验有限性）。若 A 或 B2 要求仿真阶段保证非负，需要单独决策并说明对 `docs/interface.md` §7.1 的偏离。

## 6. 实测记录

见同 PR 的说明与 `tests/performance/test_simulation_benchmark.py` 的打印输出。要点：

- 基准测试是**选择性开启**的（需 `G15_RUN_BENCHMARK=1`），因为 §7.3 明确「不要把 CI 共享 Runner 波动时间当唯一结论」。
- 默认 `pytest -q` 会跳过它，CI 保持快速且不会伪造性能结论。
- 测出的数字包含机器信息（OS / CPU / CPU 数 / Python / NumPy / 点数 / 块大小 / 计时范围）。
- **`np.savez` 那一路不是验收数字**：它用的是 NumPy 自带写入器，不是项目的原子分片写入器，也没有 Manifest 和 SHA-256。文件内已写明。

## 7. 结论

采用「块寻址 + 流隔离 + 整数时钟」的方案，可以在**不改变 `docs/interface.md` 已冻结语义**的前提下，同时满足：

- 同配置同 seed 逐点一致（§4）；
- 与分片方式解耦，支持重试与断点续跑（`members/B1/AGENT.md` 第 3 条）；
- 严格使用 `numpy.random.default_rng`（§4 字面要求）；
- 全向量化、无 Python 层逐点循环（§7.1、`members/B1/AGENT.md` 禁令）。
