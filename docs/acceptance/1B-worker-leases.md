# 1B：独立 worker 与 PostgreSQL 租约

开发起点 215a3ae；1A 测试及部署均已由维护者反馈通过。本块不追加基线检查。

## 本次交付

- 独立进程 `python -m backend.app.runtime.worker`，Compose 的 `worker` profile 默认关闭。
- 数据库时间决定领取、心跳和过期；短事务 `FOR UPDATE SKIP LOCKED`；模型等待不占领取事务。
- 每次领取增加 lease_epoch/attempt，默认心跳 5 秒、租约 30 秒、最多 3 次领取。
- 心跳在独立线程运行；过期任务可重新领取；旧 epoch 不能续租、提交任务结果或写 checkpoint/pending writes。
- 临时 Python TimeoutError/ConnectionError 最多按 5 秒/15 秒退避；已经由业务节点保存的失败 END 不自动重跑。
- SIGTERM/SIGINT 后停止领取，默认等当前任务最多 20 秒，然后停止续租并退出进程；不提前释放仍在途任务的租约。
- migration 4 `worker_presence`：worker 在线信息和领取索引；API 的 worker_available 来自数据库租约，不靠写死配置。
- 前端根据 run 状态轮询；queued 的审批执行尚未开放，隐藏执行按钮并说明原因；同步演示保持原有能力。

**边界：** 本块能执行新任务的只读采集、检索、诊断和方案生成，停在实际审批 interrupt。
复杂 checkpoint 续跑属于 2A，外部写账本属于 2B。已有 pending checkpoint 保留并明确
标为 `failed/CHECKPOINT_REQUIRES_REVIEW`，不盲目重放。已 END/interrupt 的任务只修复
状态投影。queued worker 不构造真实修改执行器，也不执行 Kubernetes 写操作。
这意味着通过 1B 后仍保留 sync 演示，不能宣称完整异步处置闭环已发布。

## 涉及文件

- `backend/app/persistence/{leases,runs,migrations}.py`：领取、CAS、数据库行锁、在线信息和迁移。
- `backend/app/runtime/{worker,checkpointer,settings}.py`：独立执行、同步调用期间心跳、检查点隔离、停机及配置。
- `backend/app/services/incident_service.py`：读取首次运行尚未写 checkpoint 的任务及在线状态。
- `compose.yaml`、`.env.example`：可选 worker 服务和默认参数。
- `frontend/src/App.tsx`、`features/incidents/polling.ts` 及对应测试：运行状态显示与轮询。
- `backend/tests/runtime/`：真实 PostgreSQL/真实 checkpoint/进程 kill 验收及错误分类用例。
- `backend/tests/run_1b_acceptance.py`、`scripts/accept_stage1b.sh`：隔离 ECS 验收入口。
- migration 测试和 1A/ADR 文档：迁移预期、1A 反馈、部署变量遗漏修正、阶段边界说明。

## 本地验证

只执行静态检查：Python AST、Git diff 检查、Bash 脚本语法。没有本地运行 pytest、
数据库、模型、Kubernetes 或 npm 测试/构建。ECS 动态结果待维护者反馈。
Checkpoint 使用 PostgresSaver 的同步 put/put_writes 扩展点；接口参考
[官方实现](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/__init__.py)。
没有升级依赖；现场 3.1.2 的具体兼容性由本块真实 checkpoint 用例验证，不能用参考代码代替运行证据。

## ECS 验收命令

```bash
cd ~/projects/k8s-incident-agent
git status --short
# 工作区干净后继续
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage1b.sh
```

脚本创建独立 `incident_agent_test_1b_*` 测试库。worker 测试各自再创建一个同前缀库，
避免前一用例留下的 queued 任务被下一用例领取；要求现有 PostgreSQL 账号可创建库
（当前 Compose 的 POSTGRES_USER 通常满足）。这些库全部保留，不清理演示库或旧证据。
测试使用真实 PostgreSQL、真实 LangGraph checkpoint 和合成工作节点，不调用模型/集群。
已有 venv、Node/npm 即可，没有新增生产依赖。脚本不启动生产 worker、不重启现有服务。

预期后端无 failed/error；真实数据库用例不能 skipped；前端测试及构建通过；迁移列表
包含 1/2/3/4，最后输出 `PASS: 1B ECS acceptance`。日志在输出提示的
`evals/results/1b/...`。正常会比 1A 多花几十秒，因为包含租约等待、长节点和进程终止。

必须验证的行为：

1. 多个领取者竞争同一任务只有一人得到任务；有效租约不会被抢占。
2. 节点执行超过初始租约仍有心跳，其他 worker 不会误抢。
3. worker 进程被 kill 后租约自然过期，原任务以新 epoch 找回；无 checkpoint 初始任务重新执行。
4. 旧拥有者的 heartbeat、最终结果、checkpoint、pending writes 及工具入口均被拒绝。
5. 重试到期前不可领取，最多 3 次；业务失败 END 保持失败，不从 START 自动重跑。
6. 真实 interrupt 保持 waiting_approval，不自动批准；pending checkpoint 不盲目续跑。
7. 停机超时后不能继续保存节点结果；在线信息过期/退出后 API 不再声称 worker 在线。

## 部署与迁移

本次先完成隔离验收并反馈，不必启用生产 worker。不要修改现有 .env 的 sync 模式。
如果要让现有 sync 演示加载 API/UI 兼容改动，使用 1A 文档修正后的部署段落：它先从
现有后端取得 KUBECONFIG_PATH 并设置 LOCAL_UID/GID，再重启后端、重建前端。
后端启动自动把该数据库增量迁移到 4；无需手工改表、无需数据库重启。

worker 需要独立部署时，在同一套已准备 Compose 环境变量的终端使用
`docker compose --profile worker up -d --no-deps worker`（先确保后端已启动并完成迁移/
checkpoint setup）。它与 backend 共用只读挂载的 backend 代码及现有镜像依赖。
修改 worker 代码后需 `docker compose --profile worker restart worker`；仅 git pull 不会
让现有 Python 进程自动加载新代码。不要在本次验收前把演示环境切到 queued。

## 尚未验证及下一块

尚待 ECS：并发、真实 checkpoint fencing、kill/停机和前端构建；生产模型/集群诊断
与 worker 容器启动不在合成测试 PASS 的证明范围内。原现场历史事件样本仍缺失。
2A 负责只读 pending task 的恢复分类和有限续跑；2B 负责审批后的外部写账本与结果核对。
收到 1B 验收反馈前不进入下一块。
