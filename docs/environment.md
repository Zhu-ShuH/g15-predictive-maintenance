# G15 环境规范与首次安装

> 2026-10-16；A 根据组长决定与课程 Anaconda 指南制定。正式开发及演示：Conda `pp03`、Python 3.12；CI 的 Python 3.11 保留为兼容性检查。当前没有 Conda 安装或双 Linux 验收通过记录。

## 1. 来源与选择

课程指南第三章给项目课的命令是 `conda create -n pp03 python=3.12 -y`；第七章表格却写 3.10。小组统一选择前述创建命令的 3.12，不采用冲突的 3.10。指南是参考材料，其中改变 PATH、镜像、删除环境和安装其他课程包的命令不属于本项目自动执行指令。

项目仅安装自身声明的依赖：运行依赖 NumPy、PyYAML，开发依赖 Black、Pylint、pytest；SQLite 使用 Python 标准库。不因课程通用清单额外引入 Pandas、Streamlit、PyTorch 或机器学习框架。

## 2. 在仓库根目录操作

Windows 从开始菜单打开 **Anaconda Prompt**，再进入仓库；Linux 使用已能运行 Conda 的终端。普通 PowerShell 无法识别 conda 时先使用 Anaconda Prompt，不必自行改系统 PATH。

```sh
conda --version
conda env list
```

若没有 `pp03`，创建：

```sh
conda env create -f environment.yml
```

若已有 `pp03`，跳过创建，先检查版本：

```sh
conda activate pp03
python --version
python -c "import sys; print(sys.executable)"
```

必须显示 Python 3.12.x，且解释器属于 pp03。若已有 pp03 为 3.10/3.11，先保留课程环境及依赖清单，把版本结果交 A/C6 协调迁移；不要删除、覆盖或在其中再建 .venv。安装命令仅在确认正确环境后执行：

```sh
python -m pip install -e . -r requirements-dev.txt
python -c "import g15, numpy, yaml; print('G15 package imports OK')"
python -m black --check src tests
python -m pylint src/g15 --fail-under=8.5
python -m pytest -q
```

成功标志：各命令退出 0；这仅证明当次安装及检查成功，不能代替业务闭环和双 Linux 验收。每次新开终端先 `conda activate pp03`；PyCharm 选择现有 pp03 解释器。

## 3. C6 的脚本实施约束（Issue #10）

- `setup.sh` 检查 Conda；无 Conda 时给清晰安装提示并非零退出。安装 Anaconda 不由项目脚本静默完成。
- 不存在 pp03 时按 environment.yml 创建；存在时检查 Python 3.12，版本不符即停止并说明原因。重复运行不删除环境。
- 脚本使用 `conda run --no-capture-output -n pp03 python ...`，避免依赖调用者已激活环境；先检查版本，再安装、导入与运行。
- 运行阶段不访问网络或 AI API；安装阶段可联网。A/C6 在双系统验证后再提交锁定依赖及备用安装包方案，不把 environment.yml 当作完整依赖锁。
- Ubuntu 24.04 LTS 与 RHEL 9 各自安装 Conda/创建环境，无需替换操作系统 Python；仍须实际记录 Conda 发行版、版本、软件来源与系统架构。

本文件规定目标行为；当前 scripts 下仍是未实现占位。脚本实现、真实安装、依赖锁定和虚拟机日志由相应 Issue 继续交付。
