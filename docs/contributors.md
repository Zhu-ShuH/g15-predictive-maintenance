# 提交身份与贡献归属

每人使用自己的克隆，在克隆内设置本仓库身份。多人共用电脑时，交接后先
核对下面的有效身份，再开始工作。角色和贡献归属依据任务、评审、成员确认
及 Git 元数据共同判断，不仅按提交数或展示名计算。

## 配置与核对

在仓库根目录运行，将名字和邮箱换成本人 GitHub 设置中核实的值：

```text
git config --local user.name "你的 GitHub 用户名"
git config --local user.email "你的 GitHub 核实邮箱"
git config --local user.useConfigOnly true
git config --local --get user.name
git config --local --get user.email
git config --show-origin --get user.email
git var GIT_AUTHOR_IDENT
git var GIT_COMMITTER_IDENT
```

`user.useConfigOnly=true` 是**仓库级配置约定，不是安全边界**：它只禁止 Git
在缺少配置时猜测身份，不会屏蔽已写入的全局 `user.name`/`user.email`。真正
生效的是 local 设置加上有效 ident 的逐项比对，所以三条命令必须都看：local
name/email、`git config --show-origin --get user.email` 的来源，以及
`git var GIT_AUTHOR_IDENT` / `GIT_COMMITTER_IDENT` 的实际结果。环境变量
（`GIT_AUTHOR_*`、`GIT_COMMITTER_*`）同样可以覆盖有效身份。上一条
`git log -1` 只能说明过去，不能验证下一次提交。

PR 创建者、提交 author、committer、实际推送账号各有含义。推送凭据需另行
核实，本地钩子无法证明使用了哪个 GitHub 账号，不读取或打印认证凭据。
不要把他人的真实贡献改成自己的名字；A 的合法代改也应保留 A 的真实署名。

### 钩子管不到的三类提交

本地 `pre-push` 只在**本机推送**时运行，以下三类不经过它，不能据钩子通过
就断定署名无误：

- 网页/API 创建或修改的提交（GitHub 生成 `<id>+<login>@users.noreply.github.com`
  形式的 noreply 邮箱）。
- Squash 合并产生的提交：主作者是 PR 创建者，分支上的其他作者写进
  `Co-authored-by`。这是真实归属，不是错署。
- 已进入 `origin/main` 或目标分支历史的提交：钩子只检查本次新增范围，
  既往贡献不重复检查。

因此 `ZSH` 与 `Zhu-ShuH` 这类同一账号的多个展示名，或 163 邮箱与 noreply
邮箱并存，**不能只凭名字或邮箱不同就判成错署**。判断要看具体账号、具体
邮箱与经审查的完整提交 SHA；**不为整个 `users.noreply.github.com` 域设
白名单**（该域服务所有用户，域名相同不证明账号相同）。

## 启用推送前检查

评审合入后，每个克隆需分别启用。先检查现有 hooksPath；若已有其他钩子，
与 A 协调组合，不能直接覆盖原有规则。Git 不会因克隆自动安装本配置。

```text
git config --show-origin --get core.hooksPath
git config --local core.hooksPath .githooks
git config --local --get core.hooksPath
git fetch origin
```

第一条在未配置时退出 `1` 是正常情况；最后一条获取 `origin/main`，供钩子
区分主线既有贡献和本次提交。`.githooks/pre-push` 在仓库中以 `100755` 和 LF
行尾保存；Linux 克隆保留执行位，Windows 需要 Git for Windows 自带的 shell。

钩子对命名 remote `origin` 的分支推送执行以下检查：

- local name/email 非空，local `user.useConfigOnly` 为 `true`；有效 author、
  committer 与 local 身份一致。
- 逐条检查本次新增提交的 author 和 committer；排除目标分支已有历史和
  已获取的 `origin/main`，避免把已合入的其他成员代码误当本人创作。
- 缺少主线基准、远端旧 tip 对象或遍历失败时拒绝推送，提示获取对象后重试；
  不自动改写历史。多个 ref 分别检查，标签和其他 remote 需单独评审。

遇到他人的合法提交，先请 A 核对来源和完整提交 SHA，并记录依据；经确认后
由该克隆显式添加一条本地例外：

```text
git config --local --add g15.identityApprovedCommit "经A审核的完整提交SHA"
git config --local --get-all g15.identityApprovedCommit
```

示例中的占位文字必须换成完整 SHA；它不是通配符，也不能填写短 SHA。
提交改写后新 SHA 需重新核对；不要把全历史批量加入例外。
本地配置可由使用者修改，所以例外列表是团队约定，不是防篡改审批凭证。
钩子允许删除分支；服务器仍按分支保护规则决定能否删除受保护分支。

## 复验与处理边界

```text
python tools/test_identity_hook.py
```

该脚本使用系统临时目录中的隔离工作库和本地 bare remote，执行真实 push，
验证错署名拒绝、合法 SHA 例外、同步主线、多 ref 和缺对象等路径；不访问
GitHub、不读取真实推送凭据、不修改当前克隆的配置或历史。需 Python 和 Git。

查找 POSIX shell 的顺序是：先按 Git for Windows 布局取
`<git>/../../bin/sh.exe`，该候选**实际存在**才使用；否则回退到
`shutil.which("sh")`；两者都找不到时明确报错退出，不静默跳过。路径字符串
本身不作为可用性证据。因此同一个脚本可在 Windows、Linux CI 与 Git Bash 中
运行，报告会打印实际使用的 shell 及其来源（`git-for-windows` 或 `path`）。

CI 在独立任务 `identity-hook` 中实际运行该脚本并保存输出，与 `skeleton`
必需检查分开；Windows 本机结果另行留证。该任务当前**不是**必需检查，服务器
保护规则不因本文件改动而调整。

钩子是本地辅助检查，不能代替服务器保护、成员确认和真实 PR 评审；
`--no-verify` 可跳过本地检查，但团队不允许用它绕过身份核对。
GitHub squash 的主作者与共同署名仍须在合并前核对，历史纠正先记录旧→新 SHA
与 tree 对照，不改写 main。C1/C6 的本机确认继续在 Issue #35 逐人验收；
本文件入仓不代表已替任何成员完成安装或确认。
