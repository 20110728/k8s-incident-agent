# 6B-2b：正式入口、worker 与审批衔接

状态：6B-2a 已由维护者反馈 ECS 通过；本块已实现，待 ECS 验收。仅实施这一个块，不提前进入 6B-3。

## 核心机制及实现位置

| 模块 | 大白话 | 具体实现 |
| --- | --- | --- |
| 新旧分流 | 新任务可走调查循环，旧任务按原流程继续 | `services/round_context.py::selected_workflow` 在创建 run 时固定 `incident-investigation-v1`；`persistence/runs.py/rounds.py` 持久化版本，worker 按记录分流，不按当前开关改变已有 run |
| 正式调查 | 接请求后由 worker 调查，HTTP 只负责保存和查看 | `runtime/worker.py::production_graph` 组装新图；`investigation/production.py::LazyToolbox/LazyModel` 延迟准备基线和模型；初始采集、RAG、预算和结果复用沿用 6B-1/2a |
| 人工等待与回答 | 问题暂停后释放 worker；用户回答才继续 | `persistence/controls.py::answer` 在原事务里保存消息、回答、幂等收据并排队；worker 将 `answers[slot]` 转成内部 Answer，先 `accept_answer` 验证并保存收据，再发送 `Command(resume=...)`；等待时间不算活动预算 |
| 参数与审批 | 模型选修复类型，程序填参数，人批准后才写 | `production.py::deterministic_plan` 仅支持既有 selector/readiness 两项；旧值取当前证据，新值取已登记 profile，调用原 `prepare_remediation_plan` 校验并绑定 UID；不增加规划模型 |
| 执行与恢复 | 保存过的决定不重新生成，写过的操作不盲目再写 | `graph.py` 将计划接原 `prepare_approval/request_human_approval/execute_remediation/verify_recovery`；沿用租约 fencing、审批绑定、操作账本及结果核对；`runtime/recovery.py` 验证版本、输入、上下文、pending 节点和审批 |
| 结果与历史 | 页面仍能看诊断、计划、审批和结果 | 新图继承 `IncidentState`，`graph.py::exported/project` 发布当前证据和原结果字段；API 只读图按 run 版本读取 checkpoint；新轮次引用截短历史消息，明确标为非当前证据 |

调用关系：创建事件/新轮次 → 保存 run → worker 精简基线采集 + 一次 RAG → 调查模型选择补采/追问/结束/候选计划 → 程序校验 → 若有计划则人工审批 → 原执行与资源/登记业务验证。

**LLM 开销：** 新路径不构造旧诊断服务和旧规划服务，不逐轮检索或额外总结；模型同时承担下一步选择和最终诊断。沿用最多 3 次调查决策、5 次实际生成调用、1 次共享纠错、6 次追加工具、40k token 预算、300 秒活动时间。详情及估算限制见 6B-1；确定性计划不花模型 token。调查用量仍在预算账本中，旧页面的诊断/规划专用计数尚未改为调查展示。

**回答边界：** 回答仍是未验证线索；等待后禁止凭旧基线宣布健康或生成写计划，需要时明确开启新一轮完整调查。`changed_resource_refs` 只接受当前 changes 问题的候选资源，任意资源返回 409；空列表兼容旧回答的幂等指纹。现有页面可以文字回答/跳过，资源勾选及调查轨迹展示留在 6B-3；本块可通过回答 API 明确提交变更资源，不从自由文本推断授权。

**停止/补充：** 复用原控制协议及事件版本，旧审批失效后不能执行。预算耗尽或结果不明时程序生成 unknown 交接，不能为了漂亮总结再调用模型。关闭开关只改变之后的新 run；已创建的新图仍用新代码恢复，不能直接回滚到不认识这个版本的旧提交。

## ECS 验收

项目目录、Python 3.12 `.venv` 已激活：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6b2b.sh
```

预期所有测试通过、零 skipped，末尾 `PASS: 6B-2b ECS acceptance.`。

脚本使用隔离 PostgreSQL 数据库、真实 checkpoint/API/worker；模型、初始采集、Kubernetes 和恢复验证响应受控，不访问真实模型或写真实集群。回归 6B-1/2a、旧对话/轮次、worker 和操作账本；新增正式创建/读取/回答、回答重放及非法对象、审批允许/拒绝、停止/补充使审批失效、新轮次上下文、等待后拒绝旧证据写计划、版本校验，以及 checkpoint 完成但发布前崩溃后在耗尽预算下仍能恢复而不重复模型/写入。

证据目录 `evals/results/6b2b/incident_agent_test_6b2b_.../`：`backend.txt`、`junit.xml`、`commit.txt`、`evidence-change.json`、`human-resampling.json`、`production-flow.json`。最后一份记录受控计划、人工决定、最终阶段和模型/写入/验证次数。测试数据库保留，失败先提供 `backend.txt`。

## 配置与部署

验收通过后，在 ECS `.env` 中设置（已有项修改，避免重复）：

```dotenv
INCIDENT_AGENT_EXECUTION_MODE=queued
INCIDENT_AGENT_INVESTIGATION_ENABLED=true
```

然后执行：

```bash
bash scripts/deploy_stage6b2b_backend.sh
```

脚本从现有 backend 取 kubeconfig 挂载路径，设置 UID/GID，重建 backend/worker 容器以载入环境变量（不是构建镜像），等待 readyz，打印两者的非敏感开关和状态。预期 readyz 为 ready，两者均 `execution_mode: queued`、`investigation_enabled: True` 且 Up。依赖代码目录挂载，与当前 ECS 部署一致；配置错配会明确报错。

不新增依赖、不新增数据库迁移（复用迁移 13）、不更新前端。默认开关 false，单纯 pull 不会启用新图；误配 true + sync 拒绝启动。需要关闭新任务入口时设 false 并再次执行部署脚本，已有新图任务仍按其保存版本完成。

本地仅静态检查，不执行 pytest、模型调用或构建。尚未验证：本块 ECS 动态结果、真实供应商结构化输出兼容与调查质量、真实集群整链路、浏览器新图交互；6B-3 才做调查过程展示和真实模型小规模对照。受控脚本通过不等于这些实测已通过。
