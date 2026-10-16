# G15 预测性维护场景沙盘

> **2026-10-16 组长决定**：采用小组默认验收口径，不再等待教师逐项答复。标准见 `docs/acceptance.md`；正式开发和演示环境为 Conda `pp03` + Python 3.12，步骤见 `docs/environment.md`。Python 3.11 保留兼容性 CI。公共 API、状态字段仍需真实评审，所有验收成绩仍须实测。

> **仅 Issue #1 仓库初始化**：当前只有包结构、占位文件与导入烟测。`docs/interface.md`、`docs/development.md` 是待评审基线，尚未冻结；仿真、消费、CLI、报告、脚本与业务测试均未实现。
>
> **依赖版本未锁定，当前不可称为可复现交付。** 本仓库尚未提供许可证；公开发布前由组长确认授权方式。

开发文档：[组员操作手册](docs/onboarding.md) · [全程路线图](docs/roadmap.md) · [九人 AI 提示词](docs/ai_prompts.md)。

## 范围与责任

正式环境为 Conda pp03 / Python 3.12；3.11 保留兼容性 CI。`src/g15/` 各模块目前是空占位；模块负责人按 `docs/development.md` 与各自 Issue 实现，不能将结构烟测当成功能验收。Ubuntu 24.04 LTS / RHEL 9 的安装运行尚未验证。

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

以上是待验证的安装与骨架检查步骤；当前 CLI 和脚本仍未实现。标准见 [验收规范](docs/acceptance.md)。依赖尚未锁定，双 Linux 尚未实测。不要提交环境目录、运行数据或密钥。

## 后续门禁

接口评审后才可以冻结公共字段、签名、CLI 与配置键。独立功能测试、性能基准、受控事件验证、两种 Linux 新克隆部署与人工 PR Review 都是后续工作，当前不存在通过记录。
