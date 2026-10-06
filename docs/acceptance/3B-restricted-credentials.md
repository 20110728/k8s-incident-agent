# 3B 交付

复验修复：兼容 Kubernetes SDK 的 `response_types_map` / `response_type` 两种调用签名，权限请求与 UID 清理共用适配；发请求前选择签名，不在写失败后重试。新增 SDK 契约回归用例，随本脚本在 ECS 执行。本地仅 AST 与 diff 静态检查。上次失败的临时资源需按该次 `reader/created-resources.json`、`remediator/created-resources.json` 和 `cleanup.json` 核对，不能按名称批量删除。

维护者已反馈 3A PASS 并重启。3B 拆分 reader/remediator 权限预期，使用临时 ServiceAccount 的短期令牌、独立 kubeconfig 实际请求 Kubernetes，并用受限 remediator 完成一次有数据库审批/操作账本的 Service 修复。

涉及：`infra/rbac/{reader,remediator}.yaml`、`scripts/check_rbac.sh`、`backend/tests/rbac/`、验收 runner 和 `scripts/accept_stage3b.sh`。reader 补齐固定业务检查器的 GET proxy 权限，与现有业务探针 Role 一致；写权限仍仅登记的 Service/Deployment。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage3b.sh
```

预期：`3B: reader/remediator real API matrices and approved repair passed.`，最后 `PASS: 3B ECS acceptance`。证据在 `evals/results/3b/`：真实身份、逐请求 HTTP 状态/拦截层、辅助 can-i、审批与修复快照、操作账本、清理结果。403 必须来自 Kubernetes 并包含对应测试身份，不以 401/404 或应用拒绝充数。

前提：现有 Python 3.12 venv、kubectl、PostgreSQL、`kind-incident-agent`；`agent-demo` 的 `incident-agent-business-probe` 已运行并 ready。当前管理凭据须能创建临时 SA/Role/ClusterRole/绑定、TokenRequest、测试 namespace、Service、Secret 和零副本 Deployment。仅新建随机 `stage3b-*` 资源，按 UID 清理，不变更现有绑定/业务对象。令牌只存于权限受限临时目录，结束后清理；独立测试数据库保留。

reader 允许本 namespace 的资源读取，不限制读取到某个资源名；remediator 只增加指定对象的 PATCH。反向测试包括其他名称的 PATCH、其他 namespace、Secrets、exec、create/delete 和 RoleBinding 创建；非法写测试使用 dry-run，exec 指向不存在的临时 Pod。正常修复为真实写。

`bash scripts/check_rbac.sh reader` / `remediator` 默认检查现有 incident-agent SA 的模拟授权；该 SA 如果同时绑定 remediator，就应选择 remediator。脚本输出仅为辅助证据。完整验收会对独立测试凭据运行两套矩阵。

无新增依赖、必填配置、数据库迁移或前端构建；本块不改应用代码，验收后无需重启。不会自动把已运行的 backend/worker 从管理员 kubeconfig 切换成受限凭据，也不会自动 apply 到现有 RBAC。

本地 Python AST（170 文件）、内部导入、三个 Bash 脚本语法及 Git diff 检查通过；未运行 pytest/npm。本地缺 PyYAML，YAML 解析及权限契约检查留在 ECS。测试只替换 remediator resourceNames 为临时对象名，其他权限来自原清单；动态结果待 ECS，不证明线上现有 SA 没有额外绑定。RBAC 不限制 PATCH 字段：登记对象的 annotation 修改在 dry-run 可通过；禁止越界修复由应用另行校验。`approver` 仍为单人演示填写标签，不是认证身份；多人认证/审批角色及真实模型、业务恢复不在本块宣称已完成。
