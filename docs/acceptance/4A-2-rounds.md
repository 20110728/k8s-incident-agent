# 4A-2：轮次与上下文（待 ECS 验收）

新增迁移 8 `incident_rounds`：复用 diagnosis run_kind、parent_run_id/input_revision，补来源消息、消息截止序号、上下文及摘要、终态结果快照。新轮独立 thread，工作流版本 `incident-round-v1`；旧 `incident-v1` 恢复路径保留。审批标识/账本绑定新轮 run_id，旧审批不能授权新轮。终态报告冻结，原 legacy 检查点不重置。

新增 `POST /api/v1/incidents/{id}/runs`，请求 `{"client_request_id":"round-1","message_id":"已保存的用户消息ID"}`，queued 模式下仅原子接受任务，返回 202；同键同消息复用，冲突 409。来源消息与 run 在同一事务关联，不修改消息原文。当前事件有活动任务、待审批、未核对写操作时拒绝；本块不撤销旧审批或处理中插话，不解析聊天意图。

`GET /api/v1/incidents/{id}/runs/{run_id}` 返回指定结果、来源消息/截止序号和冻结上下文；`.../runs/legacy` 读取原事件 thread；已有事件查询返回最新轮。上下文为最多 10 条用户消息（每条最多 600 字符）、上一轮诊断/验证摘要及来源，保留截断标识并做已有敏感字段脱敏。历史/陈述不进入当前 evidence，worker 重新执行现有采集诊断流程；本块未新增追问、动态工具选择或页面。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage4a2.sh
```

预期 `4A-2: round isolation, context, worker recovery and history passed.` 和 `PASS: 4A2 ECS acceptance`。证据在 `evals/results/4a2/`；6 个新验收用例不得跳过，另回归 4A-1、API、worker/审批/操作账本。真实 PostgreSQL/检查点/worker、确定性替身图及子进程读取；不访问模型或 Kubernetes。真实模型效果和实际业务新轮尚待后续定向验证，不能从本脚本推导。

通过后，在没有进行中的写操作时部署：

```bash
python -m scripts.init_persistence
docker restart k8s-incident-agent-backend-1
# 仅当 Compose 的应用 worker 服务已经启动时重启；不是 kind 的 incident-agent-worker 节点。
docker ps --filter label=com.docker.compose.project=k8s-incident-agent \
  --filter label=com.docker.compose.service=worker --format '{{.Names}}' | \
  while IFS= read -r name; do [ -z "$name" ] || docker restart "$name"; done
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

无新增依赖或前端构建；新增接口要求现有 `INCIDENT_AGENT_EXECUTION_MODE=queued` 并有新版应用 worker。不会自动切模式或启动 worker；sync POST 返回 409，未启 worker 的任务只会排队。迁移增量兼容旧行；存在新轮时不要回退旧 worker，它不识别新版本。旧轮审批兼容不代表可以无停机混跑不同版本 worker。

本地只做 Python AST、内部导入路径、Bash 语法、Git diff 静态检查，未运行 pytest/npm。涉及文件：rounds 路由/仓储、round_context、state/approval/context_builder、worker/recovery/leases/operations、incident_service、迁移、相关测试与验收脚本。
