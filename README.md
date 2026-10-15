# G15 预测性维护场景沙盘

> **仅 Issue #1 仓库初始化**：当前只有包结构、占位文件与导入烟测。`docs/interface.md`、`docs/development.md` 是待评审基线，尚未冻结；仿真、消费、CLI、报告、脚本与业务测试均未实现。
>
> **依赖版本未锁定，当前不可称为可复现交付。** 本仓库尚未提供许可证；公开发布前由组长确认授权方式。

开发文档：[组员操作手册](docs/onboarding.md) · [全程路线图](docs/roadmap.md) · [九人 AI 提示词](docs/ai_prompts.md)。

## 范围与责任

Python 目标版本为 3.11/3.12。`src/g15/` 各模块目前是空占位；模块负责人按 `docs/development.md` 与各自 Issue 实现，不能将结构烟测当成功能验收。Ubuntu 24.04 LTS / RHEL 9 的安装运行尚未验证。

## 待验证的开发步骤

以下命令只用于本地**骨架检查**，需要可用的 Python 3.11 或 3.12 和依赖安装网络；不是经测试的部署流程：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e . -r requirements-dev.txt
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5
python -m pytest -q
python -c "import g15"
```

在 Windows 上应使用本机虚拟环境的相应激活方式；不要把私人机器绝对路径写入仓库。`python -m g15` 和 `scripts/*.sh` 当前明确失败，待后续 Issue 由责任人实现。不要提交 `.venv/`、`runs/`、大数据或密钥。

## 后续门禁

接口评审后才可以冻结公共字段、签名、CLI 与配置键。独立功能测试、性能基准、受控事件验证、两种 Linux 新克隆部署与人工 PR Review 都是后续工作，当前不存在通过记录。
