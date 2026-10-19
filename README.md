# G15 预测性维护场景沙盘

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> **主分支状态（2026-10-18，`2c14759`）**：PR #15 已合入仿真内核、退化与噪声实现及其测试。配置/CLI、序列化、消费、分析、报告和部署脚本尚未在 main 联通；开放 PR 不等于已合并交付。`docs/interface.md`、`docs/development.md` 的待评审事项继续按对应 Issue 核对。
>
> **依赖版本未锁定，当前不可称为可复现交付。** 本仓库尚未提供许可证；公开发布前由组长确认授权方式。

开发文档：[组员操作手册](docs/onboarding.md) · [全程路线图](docs/roadmap.md) · [九人 AI 提示词](docs/ai_prompts.md)。

## 范围与责任

正式环境为 Conda pp03 / Python 3.12；3.11 保留兼容性 CI。仿真内核已有实现，其余主链仍待成员完成与集成；模块负责人按 `docs/development.md` 与各自 Issue 实现，不能将结构烟测当成功能验收。Ubuntu 24.04 LTS / RHEL 9 的安装运行尚未验证。

## 待验证的开发步骤

先按 [环境步骤](docs/environment.md) 检查是否已有 Python 3.12 的 `pp03`；不存在时，在仓库根目录执行：

```sh
conda env create -f environment.yml
conda activate pp03
python -m pip install -e . -r requirements-dev.txt
python -c "import g15"
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5
python -m pytest -q
```

以上安装步骤仍需在目标环境验证；质量检查会运行当前已有的仿真测试；当前 CLI 和脚本仍未实现。标准见 [验收规范](docs/acceptance.md)。依赖尚未锁定，双 Linux 尚未实测。不要提交环境目录、运行数据或密钥。

## 后续门禁

接口评审后才可以冻结公共字段、签名、CLI 与配置键。仿真模块已有单元测试与局部基准记录；完整生产百万点（含 NPZ、SHA-256、complete Manifest）、受控事件检出、两种 Linux 新克隆部署仍待验收。局部测试与 CI 绿灯不能替代完整闭环或目标系统证据。
