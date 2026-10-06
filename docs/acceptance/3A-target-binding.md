# 3A 交付

维护者已反馈 2C PASS；本块仅做目标、方案版本与审批绑定，不进入 3B 权限验收。

新写方案由服务端从证据绑定 `target_uid`；审批请求/记录带 `plan_revision`、`approval_revision`。修改请求、证据、方案或登记配置后，原审批不能授权新方案。补采 Pod、ReplicaSet、Deployment、EndpointSlice 关联 UID，拒绝同名不同对象及矛盾关联；同步与 queued 写入均检查目标 UID，已有发布版本检查保留。

涉及：`agent/{schemas,approval,execution_policy,remediation_policy,executor,target_identity}.py`、Kubernetes schemas/tools、profile registry、runtime operations；对应 identity/审批/执行测试和验收脚本。旧 JSON 可读，旧指纹保留；缺 UID 不补造，旧方案不自动升级，应重新采集并审批后取得完整新绑定。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage3a.sh
```

预期 `3A: all 5 real identity scenarios passed.`，最后 `PASS: 3A ECS acceptance`。包含原 persistence/API/runtime、审批执行回归、新绑定与并发审批检查；2C 故障矩阵不重复运行。日志、JUnit、批准快照、写请求和操作账本在 `evals/results/3a/`。

真实 kind 使用当前凭据在 `agent-demo` 创建/读取/修改/删除随机名 `stage3a-*` Service 和零副本 Deployment，并运行原 `stage2b-*` Service 回归；需要对应权限。仅操作测试创建的对象，按 UID 清理；保留独立测试数据库。测试 Deployment 不启动业务 Pod，不验证业务健康。

无新增依赖、必填配置、迁移（仍为 6）或前端构建。验收前无需重启。通过后：`docker restart k8s-incident-agent-backend-1`，并重启另行运行的应用 worker；不要重启 kind 节点 `incident-agent-worker`。

本地 Python AST（167 文件）、新模块内部导入、Bash 语法、Git diff 检查通过；未运行 pytest/npm，动态结果待 ECS。真实模型、完整业务恢复及受限身份权限矩阵未验证。跨 Service/Deployment 检查不是 Kubernetes 跨对象事务，检查后其他对象仍可能变化；写结果不明继续采用 2B 核对策略，不承诺跨系统恰好执行一次。
