# 4A-1：消息持久化（待 ECS 验收）

迁移 7 `incident_messages`；新增消息 schema/仓储/路由并在 main 注册。现有事件可保存消息，按事件顺序分页；同事件同角色同 client_message_id、同内容返回原记录，不同内容 409。公共 POST 只能写 user_supplied，禁止伪造角色、证据或 run；内部模型/工具消息可关联本事件 run 并按稳定 ID 去重，尚未接入模型生产路径。

`POST /api/v1/incidents/{id}/messages` 请求为 `{"client_message_id":"note-1","content":"刚发布过新版本"}`；首次 201、重复 200，返回 `message`、`created`、`processing:not_started`。这里只保存，不执行聊天指令、不触发模型/采集/审批。新 POST 要求 queued；活动 run、待审批或未核对写操作暂返回 409，待 4B 加入失效/恢复联动后放开。GET 在两种模式下均可用，旧事件返回空列表，不要求新建 run/检查点。

`GET .../messages?limit=20&before_sequence=...` 返回 items（新到旧）与 next_before_sequence；limit 1–50，使用最后一页返回的游标继续。消息内容不去除首尾空白，上限 16000 字符；本块无编辑/删除和 UI 改动。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage4a1.sh
```

预期 `4A-1: message concurrency, API, history and process persistence passed.` 与 `PASS: 4A1 ECS acceptance`。使用隔离测试数据库，不访问 Kubernetes/模型，不改演示业务记录；证据在 `evals/results/4a1/`，5 个真实数据库/API/跨进程用例不得跳过。覆盖并发去重/冲突、历史分页、旧事件、角色伪造、失败不确认保存、内部回复去重、运行中保护及进程重建后读取。

验收通过后部署：

```bash
python -m scripts.init_persistence
docker restart k8s-incident-agent-backend-1
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

迁移只增加消息表，不重写历史。无需新增依赖或前端构建；本块 worker 代码未变，不需重启。保持原执行模式，**不会自动切到 queued 或启动 worker**；消息 POST 在 sync 返回 MESSAGES_REQUIRE_QUEUED，这是预期保护。后续正式启用多轮功能前统一检查 queued/worker 配置。后台 API 启动也会补迁移，显式命令便于确认版本 7。

本地仅 Python AST、内部导入路径、Bash 语法和 Git diff 静态检查；未执行 pytest/npm。ECS 动态结果、真实重启后接口效果待维护者反馈；自动回答、轮次推进、追问和消息页面均未实现。
