# 1A：运行记录、请求幂等与任务找回

日期：2026-10-05；开发起点 699a8ca。维护者已反馈 0B 验证成功。
本次只实施 1A，等待 ECS 验收；没有实现 1B worker，也没有扩大 0A 基线。

## 这次能做什么

排队模式先把事件和任务一起存进 PostgreSQL，成功提交后才返回 202。
同一个 Idempotency-Key 和相同请求返回原任务；同键改内容返回 409。
不带键每次新建。任务可以凭键、事件 ID 或分页列表找回。
API 重启不会丢掉已接受的 queued 任务；本块不执行排队任务。

默认仍为 sync，保留原来的同步演示。sync 收到幂等键返回
409/QUEUED_MODE_REQUIRED，避免误认为同步请求也有幂等保证。
排队 API 不构造 Kubernetes/模型执行依赖，不调用图 invoke；旧 checkpoint 只读。
前端识别无 worker 的任务，说明“已保存、尚未启用后台执行”，停止持续轮询。

## 文件说明

- `backend/app/persistence/runs.py`：原子接受、唯一键冲突回读、摘要、分页游标、脱敏元数据。
- `backend/app/persistence/migrations.py`：migration 3/create_runs，活动 run 唯一约束及 1B 预留字段。
- `backend/app/services/incident_service.py`：按模式创建、queued 无 checkpoint 读取、旧事件兼容、禁止排队任务直接审批执行。
- `backend/app/api/{dependencies.py,errors.py,schemas.py,routes/incidents.py}`：只读图、错误码、run 摘要和新增查询接口。
- `backend/app/{config.py,main.py}`、`.env.example`：模式配置与 Idempotency-Key CORS。
- `frontend/src/{App.tsx,api/types.ts,features/incidents/polling.ts}`：排队任务提示及轮询停止条件。
- `backend/tests/persistence/test_{run_contracts,runs_postgres,migrations}.py`、`backend/tests/api/test_{dependencies,cors}.py`、前端 `polling.test.ts`：本块及受影响路径验收。
- `backend/tests/run_1a_acceptance.py`、`scripts/accept_stage1a.sh`：ECS 一键验收入口。

## 本地已经执行

仅静态检查：backend 下 140 个 Python 文件 AST 解析、`git diff --check`、
`bash -n scripts/accept_stage1a.sh` 语法检查。
均通过。没有本地执行 pytest、npm test/build、数据库操作或模型/集群调用。
静态检查通过不代表以下 ECS 测试已通过。

## ECS 准确命令

在原项目目录，用之前验收成功的 Python 3.12 虚拟环境执行：

```bash
cd ~/projects/k8s-incident-agent
git status --short
# 工作区干净后继续；如果有自己的改动先保留，不要 reset --hard
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage1a.sh
```

脚本复用项目 .env 的 PGVECTOR_URL 主机、端口和账号，但换成自动新建的
`incident_agent_test_1a_时间_进程号` 库。不会迁移/清理演示库，也不会重启正在运行的容器。
PostgreSQL 容器名默认就是现场的 `k8s-incident-agent-postgres-1`。
如果容器名改了，用 `PG_CONTAINER=实际容器名 bash scripts/accept_stage1a.sh`。

脚本要求现有 venv 已安装项目依赖和 pytest，宿主机有 Node/npm；缺少时会在创建库前停止。
前端统一执行 npm ci：安装锁文件里的完整开发依赖供测试/构建使用，和已经运行的 nginx 容器无关。
不新增生产依赖；使用现有 package-lock.json，不升级依赖版本。

预期：

1. 后端 pytest 汇总无 failed/error，新增真实 PostgreSQL 用例不能是 skipped。
2. 并发 8 次同键只增加一个事件和一个 run；不同内容 409；无键分别新建；写入失败 503 且无孤立事件。
3. 无 checkpoint 的 queued 事件可读；独立 Python 进程退出后，新进程按键找回相同 ID。
4. 合成旧事件使用原 thread 读取真实 PostgreSQL checkpoint；已结束 run 的同键重放仍返回原 run；执行过却丢失 checkpoint 明确报错，不重跑。
5. 分页无重复，错作用域游标和超限参数 422；列表不暴露请求正文、幂等键或 checkpoint。
6. 迁移输出 1/create_incidents、2/create_rechecks、3/create_runs；前端测试和构建成功。
7. 最后一行包含 `PASS: 1A ECS acceptance`。测试库保留，证据在输出显示的 `evals/results/1a/...`，无需手动抄事件 ID。

任何一步失败脚本立即退出；把报错及对应日志反馈，不要继续切换正式环境。
每次重跑使用新测试库，不会删除前一次证据。

## 配置、迁移和重启

- 新配置 `INCIDENT_AGENT_EXECUTION_MODE=sync|queued`，默认 sync。现阶段演示环境保持 sync，不要把正式 .env 改为 queued。
- 测试库自动增量迁移至 3；原表和历史 thread 映射不回填、不覆盖。
- 验收脚本使用独立测试进程，不占用 8000，也不需要重启现有后端。
- 若本块全部通过并希望正在运行的演示加载这次代码，执行下面命令。后端已挂载代码，只需要重启；启动时会把演示库增量迁移至 3。前端 nginx 使用构建产物，需要重建镜像。

```bash
# 确保 .env 中没有把 INCIDENT_AGENT_EXECUTION_MODE 设置为 queued
docker compose restart backend
docker compose up -d --build --no-deps frontend
curl --fail http://127.0.0.1:8000/readyz
```

预期 readyz 返回 `status: ready`。仅新增数据库表，没有数据回填，也没有新增运行依赖。
如果只是先验收后反馈，可以暂不执行这组三条部署命令。

## 尚未验证

ECS 运行测试、真实 PostgreSQL 并发/事务、真实 checkpoint、前端构建均等待维护者执行。
旧数据兼容用例使用合成历史样本；用户原来的真实旧事件仍缺少 ID，不能宣称已验证。
本块没有 worker、租约领取、kill 后继续执行或新的聊天界面；1B 验收前 queued 只保存不执行。
下一次只在收到 1A 验收结果后推进 1B。

## 2026-10-05 首次 ECS 反馈及修复

维护者在 250d241 执行后反馈：136 passed、2 failed。失败均来自验收入口
使用 make_conninfo 后产生关键字 DSN，而应用的数据库/checkpointer 入口要求 URL。
脚本在后端失败处停止，前端测试和构建尚未执行；顶部 422 弃用警告不是本次失败原因。

修复仅涉及验收入口及测试：保持 PostgreSQL URL 格式，只替换测试库名，移除可能
覆盖库名的 query dbname；保留编码后的账号密码及连接选项。增加 URL 回归用例，
将携带 DSN 的 partial 测试连接工厂换成普通函数，避免 fixture repr 显示连接密码。
本地只执行 Python AST 与 git diff --check 静态检查，运行结果仍需 ECS 复验。
无需新增依赖、修改配置、迁移演示库或重启容器。

拉取修复后重新执行 `bash scripts/accept_stage1a.sh`。仍自动新建独立测试库；
预期后端无失败，继续完成前端测试与构建，最后显示 `PASS: 1A ECS acceptance`。
