# 提交、推送与阶段存档

在已经验收的 ECS 仓库执行。交付包的完整源码是参考快照；更新现有仓库使用增量补丁，不以整个目录覆盖现场。此包不含 Git 历史、环境凭据、数据库或原始事件备份。

## 1. 应用本次文档与注释补丁

将 `k8s-incident-agent-stage2-checkpoint.tar.gz` 上传到 ECS 家目录，在实际仓库根目录执行：

```bash
task_unpack_dir="$(mktemp -d /tmp/k8s-checkpoint.XXXXXX)"
tar -xzf ~/k8s-incident-agent-stage2-checkpoint.tar.gz -C "$task_unpack_dir"
python3 "$task_unpack_dir/k8s-incident-agent-stage2-checkpoint/apply.py"
git diff --check
git diff --stat
```

每条命令成功后继续。apply.py 比较待改文件的基线摘要，校验补丁后再应用；重复执行识别为已应用。若出现 Baseline mismatch，保留已有修改和报错，不强制覆盖。补丁基于已交付并验收的只读复查阶段 2，不能直接用于最初的 89b5367 快照。

本次变更仅为文档、注释和局部格式，Python AST 保持一致。无需重跑旧故障注入、重建前端或重建集群。补丁不改变运行行为，因此不要求为本次存档重启后端。

## 2. 审查并提交

```bash
git branch --show-current
git remote -v
git status --short
git diff --check
```

以下显式暂存项目目录，包含此前阶段尚未提交的实现。先查看工作区，若还有无关开发，请只选本阶段文件；不要将无关修改混入存档。

```bash
git add README.md CHANGELOG.md docs backend scripts config infra evals/cases knowledge frontend compose.yaml pyproject.toml
git diff --cached --stat
git diff --cached --name-only
git diff --cached --check
```

确认暂存区没有环境文件、kubeconfig、密钥、依赖目录、压缩包或含敏感数据的真实事件结果。已有跟踪文件不因忽略规则而自动移出；需移出暂存区时使用 `git restore --staged -- 实际路径`，工作文件仍保留。

```bash
git commit -m "feat: checkpoint recovery verification and persisted read-only rechecks"
```

若此前实现已提交，本次仅文档与注释，可改用 `docs: document recovery boundaries and stage acceptance`。包/API 版本本次不变；不用版本字符串代替验收记录。

## 3. 推送当前分支

以下假设远端实际名为 origin；没有该远端时先配置你自己的真实仓库地址。

```bash
task_branch="$(git branch --show-current)"
if [ -n "$task_branch" ]; then
  git push -u origin "$task_branch"
else
  echo '当前为 detached HEAD，请先切换到要提交的实际分支。'
fi
git rev-parse HEAD
git ls-remote origin "refs/heads/$task_branch"
git status --short
```

推送成功后，本地 HEAD 与远端对应分支哈希应相同。认证失败或远端领先时保留本地提交，针对错误处理，不使用强制推送。

## 4. 标记阶段并导出已提交源码

确认当前提交与验收内容一致后，选择尚未使用的阶段标签。这里使用 checkpoint，避免被误认为完整 v0.2.0 发布。

```bash
task_tag="checkpoint-v02-recovery-recheck-$(git rev-parse --short HEAD)"
git tag -a "$task_tag" -m "Recovery verification and persisted manual rechecks accepted; UI and restricted-identity acceptance pending"
git push origin "refs/tags/$task_tag"
git archive --format=tar.gz --prefix=k8s-incident-agent/ --output="../k8s-incident-agent-$task_tag.tar.gz" "$task_tag"
sha256sum "../k8s-incident-agent-$task_tag.tar.gz"
```

若标签已存在先检查，不使用 `-f` 覆盖。`git archive` 仅导出标签所指的已跟踪提交，不包含尚未提交修改、Git 历史、数据库、环境变量或 kubeconfig；这是一份源码存档，不是完整环境恢复备份。数据库备份应独立保存，不提交到源码仓库。
