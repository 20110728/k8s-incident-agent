# 4B-2：追问、停止与输入仲裁（维护者已反馈验收、迁移及重启通过）

新 queued 事件及新轮使用 `incident-dialogue-v1`；旧 `incident-v1`、`incident-round-v1` 保留原恢复路径，sync 不变。unknown 诊断的 missing_evidence 若涉及开始时间、近期变更、具体报错或影响范围，按固定规则最多问 2 个、最多 2 轮，不重复问同一类；采集工具应提供的证据不转问用户。问题经 LangGraph interrupt 保存，等待释放 worker，无额外模型调用。回复恢复原 run/thread，重新采样后再诊断（即使不足 5 分钟）；跳过保持 unknown，不据此规划修复。

接口前缀 `/api/v1/incidents/{id}`：

- `GET /interaction-status` 或事件响应的 `run.question`：question_id、version、questions（slot/text）、证据版本。`adopted_message_ids` / 消息的 `adopted_by_run_ids` 区分保存与已采用。
- `POST /runs/{run_id}/answers`：client_message_id、content（最多 6000 字）、question_id、version、answers（按问题 slot 填字符串）或 `skip:true`；每条回答最多 2000 字。同键幂等，过期/已停止/另一问题回复 409；回答仍是未核实用户陈述。
- `POST /controls`：`{"client_message_id":"stop-1","action":"stop","content":"先别查了"}`；action 可为 stop/supplement/investigate。`GET /controls?client_message_id=...` 找回结果，调查请求可从 pending 变为 started 并返回 diagnosis_run_id。
- 原 `/interactions` 中明确 stop、活动/已失效轮次上的 supplement/investigate 走上述仲裁，返回 control_id；同入口支持“先别查了/停止调查/停止/stop”明确短语。活动期间其他自由文本仍需选择明确意图，普通 explain/compare 不撤销方案。原 storage-only `/messages` 保留忙时拒绝契约。

补充/停止与审批、派发锁同一事件行：派发前撤销旧授权并取消任务，旧 worker 不能继续发布；已派发则保留核对义务，不声称请求已撤回。outcome_unknown/manual_required 下继续调查保持 pending，不创建新 run 绕过。核对完成后显式调查请求才生成新轮；仅补充信息不会自动调查。停止后再继续创建新 run。原诊断/操作记录保留，API 取消状态不再展示可执行审批。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage4b2.sh
```

预期 `4B-2: durable questions, stop controls and dispatch arbitration passed.` 和 `PASS: 4B2 ECS acceptance`。11 个新用例不得跳过：真实 PostgreSQL、检查点、跨进程继续、幂等/过期回复、停止竞争、审批竞争、派发前后写边界；回归 4A、4B-1 确定性用例及 worker/审批账本。模型、采集与 Kubernetes 写响应为替身，不调用真实模型或修改真实集群。失败提供 `evals/results/4b2/<测试库>/backend.txt` 和 `junit.xml`。

验收通过且没有进行中的修复时部署：

```bash
docker stop k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
python -m scripts.init_persistence && \
  docker start k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

新增迁移 10 `dialogue_controls`；维持 queued，无新增依赖、配置或前端构建。backend/worker 必须一起更新，不混跑旧 worker；已有 `incident-dialogue-v1` 等待任务时不可回退旧程序。尚待 ECS 动态验收和真实模型/现场端到端交互；固定问法不等于阶段 6 的自主工具循环。4C 页面尚未开发。
