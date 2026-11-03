# G15 预测性维护场景沙盘

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> **主分支状态（2026-11-03，`6375e2ef`）**：仿真内核、序列化、生产、消费、分析、存储、报告与部署脚本均已在 main 联通。CLI 的 `validate` / `consume` / `benchmark` 已实现，`simulate` / `report` / `verify` / `doctor` 四个子命令仍未实现；`verify.sh` 的仿真阶段与 `demo.sh` 因 `simulate` 未实现而按设计非零退出。开放 PR 不等于已合并交付。`docs/interface.md`、`docs/development.md` 的待评审事项继续按对应 Issue 核对。
>
> **依赖版本已锁定（`#95` 已合入 `9c54d3ea`）**：`requirements.txt` 与 `requirements-dev.txt` 均引用 `requirements.lock.txt`（22 项，NumPy 通过 PEP 508 标记在 Python 3.11 解析为 2.4.6、在 Python 3.12 解析为 2.5.4）。但 `#28` 的双 Linux 复核仍未完成，`--no-deps --no-build-isolation` 已由 PR #102 接入 CI（`ci.yml:20-24` 拆两步 + 一步校验——① 锁安装 ② 项目安装 `--no-deps --no-build-isolation` ③ `pip check`；#94 AC1–AC5 全部达成，待 AC6 即本 README 同步后关单）——锁定生效不等于可复现安装已验收。本仓库尚未提供许可证；公开发布前由组长确认授权方式。

开发文档：[组员操作手册](docs/onboarding.md) · [全程路线图](docs/roadmap.md) · [九人 AI 提示词](docs/ai_prompts.md)。

## 范围与责任

正式环境为 Conda pp03 / Python 3.12；3.11 保留兼容性 CI。主链的生产、消费、分析、存储与报告模块均已实现并合入 main；**尚未完成的是四个 CLI 子命令的接线与正式验收**，不是主链未实现。模块负责人按 `docs/development.md` 与各自 Issue 实现，不能将结构烟测当成功能验收。Ubuntu 24.04 LTS / RHEL 9 的安装运行已在两机跑通（见下「待验证的开发步骤」），但**正式验收与检出率结论**尚未完成。

## 待验证的开发步骤

先按 [环境步骤](docs/environment.md) 检查是否已有 Python 3.12 的 `pp03`；不存在时，在仓库根目录执行：

```sh
conda env create -f environment.yml
conda activate pp03
python -m pip install -r requirements-dev.txt
python -m pip install -e . --no-deps --no-build-isolation
python -c "import g15"
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5
python -m pytest -q
```

以上安装步骤仍需在目标环境验证；质量检查会运行当前已有的测试。当前 CLI 已实现 `validate` / `consume` / `benchmark`，`simulate` / `report` / `verify` / `doctor` 四个子命令尚未实现；部署脚本（`setup` / `verify` / `demo` / `benchmark` / `clean`）已就位，其中 `verify` 的仿真阶段仍因 `simulate` 未实现而阻塞，`demo` 同样按设计诚实失败。标准见 [验收规范](docs/acceptance.md)。依赖已锁定（`#95` 已合入）；两机（RHEL 9.7 / Ubuntu 24.04.4）已从公开仓库现场全新克隆 `2054bb59` 并跑通 `setup.sh` 与 `benchmark.sh`（#10），但正式验收、检出率与峰值内存结论仍未完成。不要提交环境目录、运行数据或密钥。

## 后续门禁

接口评审后才可以冻结公共字段、签名、CLI 与配置键。仿真模块已有单元测试与局部基准记录；百万点生产基准已在双 Linux 取得计时与峰值 RSS 数字并登记进 `docs/test_plan.md` / `docs/test_report.md`，但完整生产百万点闭环、受控事件检出与两种 Linux 的正式验收仍待验收（现场全新克隆演示已由 #10 补证）。局部测试与 CI 绿灯不能替代完整闭环或目标系统证据。
