# 4B-1：交互路由与只读回答（维护者已反馈 ECS 验收、迁移及重启通过）

本块：状态查询、历史解释/对比、保存补充信息、继续调查、只读复查。新增 `interaction` 任务，复用 worker 租约；不作为事件的最新诊断。消息与任务原子接受，模型调用记录用途、耗时、tokens（中断未返回时标记未知），已保存回答/观察在恢复时复用。继续调查的子轮次与交互结果同事务提交；聊天不能授权修复。

接口前缀 `/api/v1/incidents/{id}`：

- `GET /interaction-status`：只查存储，不调模型/集群。
- `POST /interactions`：`{"client_message_id":"唯一重试键","intent":"auto","content":"我改了配置，请重新检查"}`；非 status 返回 202，HTTP 不执行模型。intent 可选 auto/status/explain/compare/supplement/investigate/recheck。明确 status/supplement/recheck/investigate 不需要路由模型；auto 会调用路由模型。
- `GET /interactions?client_message_id=...` 或 `GET /interactions/{run_id}`：找回/轮询；同键同请求复用，不同请求 409，失败后要重新尝试须用新键。
- explain 可选 `reference_run_id`（默认最新诊断，`legacy` 指原事件）；compare 另传 `compare_run_id`，不足两轮只提示选择。引用限定同一事件；回答展示证据 ID、采样时间、快照时间及未知项。输入摘录有限，引用存在性校验不等于事实推理完全正确。

活动调查时拒绝新交互；待审批时仅明确 explain/compare 可读冻结快照，其余仍 409。自动追问、停止、补充事实使旧审批失效留给 4B-2；页面留给 4C。旧 recheck API 保持原契约，新入口观察按任务去重；未保存的模型调用/只读采样在崩溃后可能重做，不承诺外部调用恰好一次。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage4b1.sh
```

预期 `4B-1: durable routing, historical answers, rechecks and live model passed.` 与 `PASS: 4B1 ECS acceptance`。9 个新用例不准跳过，回归 4A 消息/轮次、API、持久化、worker、审批账本；保留隔离测试库。证据：`evals/results/4b1/<测试库>/backend.txt`、`junit.xml`、`live-model.json`、`migrations.txt`。真实模型正常路径 5 次调用，使用已有 DashScope 配置；集群观察为替身，不改真实 Kubernetes 资源。

验收通过后，在没有进行中的写操作时部署（短暂中断 API）：

```bash
docker stop k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
python -m scripts.init_persistence && \
  docker start k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

迁移新增 9 `interaction_tasks`，旧诊断/消息/结果保留；不要混跑旧 worker 或在存在交互任务时退回旧版。维持 `INCIDENT_AGENT_EXECUTION_MODE=queued`；无新增依赖、环境变量或前端构建。上述启动沿用已有容器配置和后端代码挂载，不依赖 Compose 的 UID/kubeconfig 插值。

涉及：interactions 路由/仓储/执行器、interaction_schemas、LLM interaction、worker、rounds/runs 查询、迁移及验收用例。其他文件只作接口兼容/入口注册。本地仅完成 Python AST、项目导入路径、Bash 语法、diff 静态检查；未运行 pytest/npm/模型/数据库。尚待 ECS 验收及新入口连接真实业务集群的实测，不由 readyz 或测试替身推导。
