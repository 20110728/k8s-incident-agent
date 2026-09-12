# 在 ECS 提交和推送当前快照

以下命令在已经验收的 ECS 仓库执行。文档/注释补丁不包含 Git 历史、远程仓库连接或凭据；应用补丁不会自动提交和推送。

## 1. 应用补丁

将下载的 `k8s-incident-agent-v02-docs-comments.tar.gz` 上传到 ECS 的 `~/` 后：

```bash
cd ~/projects/k8s-incident-agent
tar -xzf ~/k8s-incident-agent-v02-docs-comments.tar.gz -C /tmp
git apply --check /tmp/k8s-incident-agent-v02-docs-comments/docs-comments.patch
git apply /tmp/k8s-incident-agent-v02-docs-comments/docs-comments.patch
```

每条命令成功后再执行下一条。补丁基于已交付的阶段 5 源码；如果检查报冲突，先保留当前文件和错误输出，不要强制覆盖。尤其是已有自定义 README 或 `.gitignore` 时需要合并。

## 2. 检查并提交

先确认当前分支、远端与工作区；远端名称以实际输出为准。

```bash
git branch --show-current
git remote -v
git status --short
git diff --check
```

下面显式暂存当前 v0.2 的主要源码、配置、固定案例和文档，包含此前阶段尚未提交的内容：

```bash
git add README.md CHANGELOG.md .gitignore docs/CODE_READING.md docs/GIT_PUBLISH.md
git add backend scripts config infra evals/cases knowledge frontend compose.yaml pyproject.toml
git diff --cached --stat
git diff --cached --name-only
git diff --cached --check
```

检查暂存区：不要包含 `.env`、kubeconfig、凭据、依赖目录、压缩包或真实采集结果。新增忽略规则不会取消已跟踪文件；若暂存区存在不应提交的文件，用 `git restore --staged -- <实际文件路径>` 移出暂存区，该操作保留工作文件。

确认暂存内容后：

```bash
git commit -m "feat: complete v0.2 stage 1-5 snapshot and document acceptance scope"
```

不需要因为只增加文档和注释而重跑全部已验收场景。本次补丁已做 Python 语法树等价检查；如果合并时修改了执行代码，应针对修改点验证。

## 3. 推送并核对

以下命令适用于已配置名为 `origin` 的远端；如名称不同，用实际远端名替换。不要猜测仓库地址，也不要强制推送。

```bash
task_branch="$(git branch --show-current)"
```

确认 `task_branch` 非空，即当前不是 detached HEAD，然后执行：

```bash
git push -u origin "$task_branch"
git rev-parse HEAD
git ls-remote origin "refs/heads/$task_branch"
git status --short
```

推送成功后，本地 HEAD 与远端分支的提交哈希应相同。推送因认证或远端领先失败时，提交仍保留在本地；先处理具体错误，不使用 `--force`。

没有 `origin` 时，需要先用实际仓库地址配置远端。此快照尚未完成阶段 6–8，不建议标记为完整 `v0.2.0` 正式发布。
