# G15 组员一步一步操作手册

> 本文状态：草案／待评审；适用对象：A、B1、B2、C1–C6（包括第一次使用 Git 的组员）；更新日期：2026-10-16。
>
> 当前仅 Issue #1 仓库骨架已合并。业务模块、可用场景配置、CLI 与部署脚本尚未实现；以下步骤是开发协作流程，不表示端到端功能现在能运行。`docs/interface.md` 和 `docs/development.md` 是待组内评审的设计基线，不能称为已冻结。依赖版本未锁定，不能称为可复现交付。

**怎么读命令**：除标明平台的命令外，`git` 命令在 Windows Git Bash、Windows PowerShell 和 Linux 终端相同；所有命令在**仓库根目录**运行，步骤 0 的安装、克隆除外。代码块里的命令可复制；遇到要求输入名字或仓库地址的提示，粘贴自己的值，不要把个人信息写入文档。命令返回码 `0` 表示命令成功，非 `0` 表示需要排查，不等于业务验收成功。Windows 用 Git Bash 运行 `bash` 脚本；PowerShell 如需调用仓库脚本也使用已安装 Git 提供的 `bash`，不要用 `./scripts/*.sh`。仓库当前 `scripts/*.sh` 在 Git 索引中均为 `100644`、没有可执行位；脚本本身也会报告“未实现”并以 `1` 退出。可执行位已由 A 单独开 Issue 交 C6 修复，本次不改。

**术语首次解释**：Git 是记录文件版本的工具；GitHub 是存放公开仓库并协作审查的网站；仓库是包含项目代码和版本历史的文件夹；`main` 是共享主分支；分支是从主分支分出的独立工作线；Issue 是写清任务和验收条件的工单；PR（Pull Request）是请求把分支改动合并进 `main` 的审查单；CI（持续集成）是提交后自动运行的检查；Review 是组员人工阅读并批准或要求修改 PR。

## 0. 开始之前：装工具、配置身份、克隆仓库

1. 从官方发行渠道安装 **Git** 与 **Python 3.11 或 3.12**；Windows 另安装 **GitHub Desktop**，它是可视化查看改动的辅助工具，终端命令仍以本手册为准。Linux 按本手册使用 Git 终端，不要求 GitHub Desktop。Windows 安装 Git 时启用 Git Bash；Linux 的 Git 和 Python 用对应发行版软件源安装，先确认发行版可提供符合要求的 Python，不能假设 RHEL 默认具备所需解释器。安装后验证：

   ```sh
   # Windows Git Bash / PowerShell
   git --version
   py -3.11 --version
   # 若只安装了 3.12，则把上一行改为：py -3.12 --version
   ```

   ```sh
   # Linux
   git --version
   python3.11 --version
   # 若只安装了 3.12，则把上一行改为：python3.12 --version
   ```

   ✅ 成功标志：Git 显示版本号；所选 Python 命令显示 `3.11.x` 或 `3.12.x`。Windows 上 GitHub Desktop 能打开；Linux 只需终端工具。

   如果 Windows 报 `No suitable Python runtime found`，先运行 `py -0` 查看实际注册的解释器，使用列表中真实可用的 Python 3.11/3.12 命令；没有符合版本的解释器时先安装，不反复照抄不存在的 `py -3.11`。

2. 设置自己的 Git 作者身份。Windows Git Bash / Linux：

   ```sh
   read -r -p '输入用于提交的名字: ' GIT_NAME
   read -r -p '输入用于提交的邮箱: ' GIT_EMAIL
   git config --global user.name "$GIT_NAME"
   git config --global user.email "$GIT_EMAIL"
   git config --global --get user.name
   git config --global --get user.email
   ```

   Windows PowerShell：

   ```powershell
   $gitName = Read-Host '输入用于提交的名字'
   $gitEmail = Read-Host '输入用于提交的邮箱'
   git config --global user.name "$gitName"
   git config --global user.email "$gitEmail"
   git config --global --get user.name
   git config --global --get user.email
   ```

   ✅ 成功标志：最后两行回显你输入的身份；用你有权公开的提交邮箱，不要填写 Token。

3. 在 GitHub 仓库页面点击 **Code**，复制该仓库的 HTTPS 地址。在打算存放项目的**父文件夹**打开终端，粘贴地址克隆（`clone` 意为下载仓库及版本历史）；本手册不写个人账号或路径。Windows Git Bash / Linux：

   ```sh
   read -r -p '粘贴仓库 Code 页面中的 HTTPS 地址: ' REPO_URL
   git clone "$REPO_URL"
   cd g15-predictive-maintenance
   git status --short --branch
   ```

   Windows PowerShell：

   ```powershell
   $repoUrl = Read-Host '粘贴仓库 Code 页面中的 HTTPS 地址'
   git clone "$repoUrl"
   Set-Location g15-predictive-maintenance
   git status --short --branch
   ```

   ✅ 成功标志：进入名为 `g15-predictive-maintenance` 的仓库，状态显示 `main`，没有意外改动；如果 Git 要求认证，按 GitHub 正常登录流程操作，不把凭据写进仓库。后续每次打开终端，先进入这个仓库根目录。

4. **把个人操作手册、教师资料、密钥放在仓库文件夹外**，例如与仓库同级的 `g15-manuals/`，不要复制进仓库、`docs/` 或 `runs/`。用下列命令检查 Git 即将看到什么：

   ```sh
   git status --short --untracked-files=all
   ```

   ✅ 成功标志：列表里没有教师资料、个人操作手册、密钥或大数据。公开仓库里一次 `git add -A` 可能把未忽略的私人文件一起加入提交；发现此类文件先停止提交、移出仓库并核查暂存区，不用 `git add -A` 图省事。

## 1. 每天开工第一件事：同步 main

1. 确认自己已提交或妥善保留当前分支改动，再切到主分支并拉取最新代码。Windows Git Bash / Linux：

   ```sh
   git status --short
   git switch main && git pull
   git status --short --branch
   ```

   Windows PowerShell（不能照抄 Bash 的 `&&`）：

   ```powershell
   git status --short
   git switch main
   if ($?) { git pull }
   git status --short --branch
   ```

   ✅ 成功标志：`main` 已同步且无冲突，状态干净；若 `git switch` 提示会覆盖本地改动，**不要强制切换**，先保留改动并向负责人询问。

## 2. 领任务：读 Issue、角色边界和接口

1. 从 GitHub Issues 找到分配给自己的 #2–#11 中的任务，读“修改范围、输入输出、依赖、验收条件”。角色文件定义的是**长期白名单**；Issue 的“修改范围”定义的是**本次范围**，两层都要满足。用以下命令确认本地文件；Windows Git Bash / Linux：

   ```sh
   git ls-files 'members/*/AGENT.md' 'docs/interface.md' 'docs/development.md'
   ```

   Windows PowerShell：

   ```powershell
   git ls-files 'members/*/AGENT.md' 'docs/interface.md' 'docs/development.md'
   ```

   ✅ 成功标志：能列出你的 `members/<你的角色>/AGENT.md` 和两份规范；随后在编辑器中打开自己的文件，并按 Issue 阅读 `docs/interface.md` 的准确小节：§4 配置、§5 数据交换、§6 模块接口、§7 仿真、§8 分析、§9 SQLite、§10 CLI、§11 报告、§12 部署、§14 追踪。`<你的角色>` 是阅读指引，不是要直接执行的文件名。

## 3. 建自己的分支

1. 在同步后的 `main` 上按 `<类型>/<issue编号>-<英文短描述>` 新建分支；类型选 `feat`（新功能）、`fix`（修复）、`test`（测试）、`docs`（文档）。以下以 #5 为例，各终端相同：

   ```sh
   git switch -c feat/5-simulation-engine
   git status --short --branch
   ```

   ✅ 成功标志：当前分支显示 `feat/5-simulation-engine`，而不是 `main`；做别的 Issue 时替换编号和短描述，**绝不直接向 `main` 推代码**。

## 4. 建本地虚拟环境 `.venv` 并装依赖

虚拟环境是只给当前项目用的一套 Python 包，不与系统 Python 混用。`requirements-dev.txt` 引用 `requirements.txt` 并包含 Black、Pylint、pytest；目前**没有锁版本**，安装结果不能称为可复现交付。

1. Windows Git Bash / PowerShell（以 Python 3.11 为例，只装 3.12 就把 `-3.11` 改为 `-3.12`）：

   ```sh
   py -3.11 -m venv .venv
   .venv/Scripts/python.exe -m pip install -e . -r requirements-dev.txt
   .venv/Scripts/python.exe -c "import g15"
   ```

   ✅ 成功标志：存在 `.venv/`、安装命令退出 `0`、导入命令退出 `0`；这是**包结构导入**，不是业务可用证明。这里直接调用虚拟环境解释器，不要求激活。

2. Linux（下面示例用 3.11；只有 3.12 时换成 `python3.12`）：

   ```sh
   python3.11 -m venv .venv
   .venv/bin/python -m pip install -e . -r requirements-dev.txt
   .venv/bin/python -c "import g15"
   ```

   ✅ 成功标志：同上。缺少 `venv` 模块时先安装对应发行版的 Python venv 包，再重试；不要假定 `bash scripts/setup.sh` 现在能代劳，它目前是退出 `1` 的占位脚本。

## 5. 写代码与测试：只改这次获准的文件

1. 对照 Issue “修改范围”和角色长期白名单，修改自己负责的源码；相应的新测试文件若不存在，应按 Issue **新建**，不要当成现有测试。每次改动后查看清单：

   ```sh
   git status --short --untracked-files=all
   git diff --check
   ```

   ✅ 成功标志：只出现本 Issue 允许的文件；`git diff --check` 返回 `0`，无行尾空格等问题。不得私改公共 Schema、CLI 参数/退出码、配置键、评分口径、依赖或测试阈值；有冲突先向 A 提方案并记录评审。

2. 按 Issue 补对应边界测试，再查看具体测试文件是否已经创建（以下只是查看目录，不假定某业务测试现存）：

   ```sh
   git status --short --untracked-files=all
   ```

   ✅ 成功标志：能说清测试输入、预期和文件归属；当前基线仅有 `tests/test_package_import.py` 包导入烟测，不能把它当业务测试。

## 6. 本地质量门禁：逐项运行、记录真实退出码

以下示例默认步骤 4 已建立 `.venv`。Windows Git Bash / PowerShell 使用 `.venv/Scripts/python.exe`，Linux 使用 `.venv/bin/python`；下文 Windows 和 Linux 各列一套，不能混用解释器。`--fail-under=8.5` 是当前 Pylint 门槛，未经审批不得放宽。

1. 检查 Black 格式（`--check` 只检查，不改文件）：

   ```sh
   # Windows Git Bash / PowerShell
   .venv/Scripts/python.exe -m black --check src tests
   # Linux
   .venv/bin/python -m black --check src tests
   ```

   ✅ 成功标志：**只运行所属平台一行**且退出码 `0`；报 `would reformat` 时，在自己获准的文件上运行对应解释器的 `-m black <获准文件>`，看 diff 后重跑检查，不能顺手格式化别人的模块。

2. 检查 Pylint 静态质量：

   ```sh
   # Windows Git Bash / PowerShell
   .venv/Scripts/python.exe -m pylint src/g15 --fail-under=8.5
   # Linux
   .venv/bin/python -m pylint src/g15 --fail-under=8.5
   ```

   ✅ 成功标志：所属平台命令退出 `0`；非 `0` 时读输出中的**文件、行号和规则**，修获准文件；问题来自他人模块则记录并通知负责人，不擅自改别人文件或降门槛。

3. 运行测试（pytest 是运行 Python 自动化测试的工具）：

   ```sh
   # Windows Git Bash / PowerShell
   .venv/Scripts/python.exe -m pytest -q
   # Linux
   .venv/bin/python -m pytest -q
   ```

   ✅ 成功标志：所属平台命令退出 `0` 且失败数为零；当前仅有结构烟测，即使通过也**不能**写成功能、性能、双 Linux 或闭环通过。非 `0` 看第一处失败的断言与 traceback（错误调用栈），先复现再修复；没运行就在 PR 写“未运行”及原因。

## 7. 提交：写得出原因的 Conventional Commits

Conventional Commits 是 `类型(模块): 做了什么` 的提交说明格式，例如 `feat(sim): add seeded noise`、`test(schema): reject invalid hash`、`docs(guides): add onboarding workflow`；反例：`修改`、`测试一下`、`AI 写的代码`，都无法追溯目标。

1. **只暂存本 Issue 明确获准的文件**；示例为 #5，真实文件由负责人确认：

   ```sh
   git add src/g15/simulation/engine.py src/g15/simulation/degradation.py src/g15/simulation/noise.py
   git diff --cached --stat
   git diff --cached --check
   ```

   ✅ 成功标志：暂存区仅有本次获准文件，检查退出 `0`；要提交测试文件时**明确写出其真实文件名再 `git add`**，不盲目 `git add -A`。

2. 检查暂存内容无机密后提交：

   ```sh
   git diff --cached
   git commit -m "feat(sim): implement seeded simulation engine"
   git status --short --branch
   ```

   ✅ 成功标志：提交成功且状态符合预期；提交前看 diff 不含个人资料、教师材料、Token、生成的大文件或私有路径。文档任务可改用 `docs(guides): ...`，不要照搬示例提交不相干内容。

## 8. 推送并开 PR

1. 推送的是**当前任务分支**，不是 `main`：

   ```sh
   git branch --show-current
   git push -u origin HEAD
   ```

   ✅ 成功标志：显示自己任务分支，推送成功并建立上游关系；推送失败不要强推。PR 尚未建立时，推送成功不等于已合并。

2. 在 GitHub 仓库页面选 **Compare & pull request**，目标 `main`、来源为自己的分支，按 `.github/pull_request_template.md` 逐项填写：改动原因、修改文件/公共接口、真实命令和退出码、自检、风险与回退。普通 #2–#11 任务填对应的 `Closes #<真实 Issue 编号>`，这样合并时可关闭 Issue；查看模板：

   ```sh
   git ls-files .github/pull_request_template.md
   ```

   ✅ 成功标志：文件路径存在，网页 PR 的 base/head、变更文件与 Issue 编号均正确；不要把“未运行”改写成“通过”。普通任务填 `Closes #<真实 Issue 编号>`；本次文档任务填 `Closes #12`。

## 9. 等 CI、请人 Review、按意见修改

1. PR 页查看 **Checks**；绿色勾表示对应自动检查成功，红叉表示某项失败，不代表全部项目已验收。当前工作流只有 `skeleton (3.11)` 和 `skeleton (3.12)` 两个骨架检查 job；线上 2026-10-15 的 3 次运行均为 `completed / success`，但不能外推为功能完成。发现红叉，点失败 job → 失败 step → 日志中**第一条真正错误**；本地复现第 6 节对应命令。先确认目前分支：

   ```sh
   git status --short --branch
   ```

   ✅ 成功标志：定位到具体失败命令及文件/行号，写在 PR 评论；修复后再次推送并等新一轮 Checks 全绿，不拿旧绿勾替代。

2. 邀请**至少 1 位真人** Review；涉及 Schema、配置键、CLI 或依赖等公共契约的 PR 还需 A 审。逐条回复审查评论：说明修改在哪个提交，或给出理由与证据；修完用本地质量门禁复检：

   ```sh
   git status --short --branch
   git push
   ```

   ✅ 成功标志：最新提交已推送、PR 的最新 Checks 全绿、至少一位真人批准；AI 自动意见不算人工审批。若缺少证据或存在未解决意见，先不要合并。

## 10. 合并后收尾

1. 确认 PR 在 GitHub 页面显示 **Merged**，再切回并同步 `main`：Windows Git Bash / Linux：

   ```sh
   git switch main && git pull
   git status --short --branch
   ```

   Windows PowerShell：

   ```powershell
   git switch main
   if ($?) { git pull }
   git status --short --branch
   ```

   ✅ 成功标志：本地 `main` 已包含 PR 提交且工作区干净；不能仅凭“关闭 PR”判断已合并。

2. **只删除自己已经合并的任务分支**。以第 3 节示例为例，先核对名称；Git Bash / PowerShell / Linux 相同：

   ```sh
   git branch --merged main
   git branch -d feat/5-simulation-engine
   git push origin --delete feat/5-simulation-engine
   ```

   ✅ 成功标志：核对该分支确实在已合并列表内，本地与远端任务分支删除成功；没合并或不属于自己的分支不删，绝不删除 `main`。

## 11. 常见报错速查表

遇到报错先在**仓库根目录**确认状态，不要盲目复制网上的 `--force`、`reset --hard` 或清理命令：

```sh
git status --short --branch
```
✅ 成功标志：知道自己当前在哪个分支、有哪些未提交改动，再选以下相应处理；“没有输出”通常表示状态干净，而不是已经完成任务。

| 报错/现象 | 含义与安全做法 |
|---|---|
| `Author identity unknown` | Git 没有可用作者身份。回到 §0 第 2 步设置 `user.name` 和 `user.email`，再重试提交；不要把别人的名字填成自己的。 |
| `! [rejected] main -> main (fetch first)` | 曾试图推送主分支且远端已有新提交。**停止推 main**，保留本地改动、向 A 报告并在任务分支继续；不要强推，别用硬重置丢改动。 |
| `remote origin already exists` | 本地已有名为 `origin` 的远端。先 `git remote -v` 看地址是否正确；正确就不要重复添加，错误先向 A 核实再修改。 |
| CI 变红 | 在 PR 的 Checks 点红色 job → 失败 step → 日志第一处具体错误；用 §6 对应命令本地复现，记录命令、退出码、文件和行号，再修自己有权限的文件。 |
| `pytest` 找不到 `g15` 包 | 确认位于仓库根目录、使用了步骤 4 的 `.venv`，执行本平台 `...python -m pip install -e . -r requirements-dev.txt` 后重试；不要借改测试或私自增 `sys.path` 掩盖安装失败。 |
| Black `would reformat` | 在获准文件上使用本平台 `...python -m black <获准文件>`，检查 diff 后重跑 `--check`；无权修改的文件向负责人报告。 |
| `bash scripts/verify.sh` 或 `python -m g15` 报“未实现” | 这是当前骨架的**预期占位失败（退出码 1）**，不能报告成验收通过；等待相应 Issue 实现。**在可执行位修复前**，一律以 `bash scripts/xxx.sh` 调用（不要用 `./scripts/xxx.sh`）；修复后两种方式均可，但文档仍以 `bash` 形式为准，跨平台更稳。 |

## 12. 组长 A：简短审 PR、合并、保护分支、关闭 Issue

1. A 在网页 PR 的 **Files changed** 对照 Issue 修改范围、角色白名单和接口章节，核验测试证据、风险与回退；需要修正就提交 Review 评论，不用 AI 意见代替真人批准。先在本地看本次变更范围：

   ```sh
   git status --short --branch
   ```

   ✅ 成功标志：PR 有至少 1 位真人批准、最新 CI 绿色、所有必须的 A 审查已完成；无证据就暂缓合并。

2. A 在 GitHub 仓库 **Settings → Branches → Add branch protection rule** 为 `main` 要求 PR、至少 1 位审批和必要状态检查（当前可见名称 `skeleton (3.11)`、`skeleton (3.12)`），限制直接推送；`CODEOWNERS` 目前只是注释占位，不能当成已生效规则。用命令核实本地基线分支：

   ```sh
   git branch --show-current
   ```

   ✅ 成功标志：网页保存的规则明确作用于 `main`，真正阻止直接推送；本地命令仅帮助核对分支，**不能证明线上规则已启用**，必须在网页验证。

3. 所有门禁满足后由 A 在 GitHub 网页合并 PR，核对关联 Issue 是否已关闭；若 `Closes #编号` 未生效，核对实际 Issue 再在网页手动关闭并写明 PR 链接。本次文档任务关联已存在的 Issue #12，只有 PR #14 合并并实际关闭该 Issue 后才标记完成。合并后同步：

   ```sh
   git switch main
   git pull
   ```

   ✅ 成功标志：网页 PR 状态为 **Merged**、对应真实 Issue 为 **Closed**、本地 `main` 已更新；关闭 Issue 不代表功能门禁自然通过。
