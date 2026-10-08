# 6B-2a：持久化追问与有依据复采

状态：已实现，待 ECS 验收。维护者已反馈 `PASS: 6B-1 ECS acceptance.`。

6B-2 分两块验收：本块实现调查图内部的人工等待/恢复、回答幂等和复采策略；下一块 6B-2b 接正式入口、worker/历史版本分流、现有回答 API，并组装确定性修复计划衔接审批执行。本块不改变浏览器和正式事件路由，旧图默认行为保留。

## 核心机制与代码

### 1. 模型何时追问，图怎样等待

大白话：模型可以问“什么时候开始、改过什么、用户看到了什么、影响哪些请求”。程序只允许这些人工信息类别，同一类别不反复问。问题发出后图暂停，回答到来再继续原来的调查。

`contracts.py::Ask.slot` 限定 `onset/changes/symptom/impact`；`dialogue.py::question_for` 校验重复类别和两轮上限，按 run ID/问题序号生成稳定 question_id，绑定当时的证据摘要。追问本身使用原调查决策预算，最多 3 次调查决策、5 次实际生成调用不变。

`graph.py` 仅在 `interactive=True` 时增加 `await_investigation_input` 节点，并要求 checkpointer。决策节点先输出问题，LangGraph 保存检查点；等待节点再调用 `interrupt(question)`。等待节点在 interrupt 前不调用模型、工具或创建预算预留，恢复重进节点不会再次花钱生成问题。

“问题是否对排障有帮助”仍由模型判断和后续实测评估。程序能限制信息类别、次数及权限，不能证明自由文本一定问得有价值。

### 2. 回答怎样恢复，为什么不能直接信任

大白话：回答必须对应当前问题；说“修好了”只是用户提供的线索，不是集群检查结果。重复点提交不应重复调查。

`dialogue.py::advance_interactive` 是本块内部恢复入口，检查工作流版本和 baseline，再验证回答，最后才调用 `Command(resume=...)`。无效回答不进入 LangGraph 的恢复值，可以更正后重新提交。不要绕过它直接把外部输入传给 `graph.invoke(Command(...))`。

`accept_answer` 校验 question_id/version、非空文本、skip 互斥和变更资源引用。在现有 `run_budgets.payload.investigation_answers` 内保存问题编号、回答指纹、消息编号、服务端接收时间和原始回答；同一回答返回原收据，更改已回答内容或跨问题重用消息编号会被拒绝。收据先落盘，之后图才推进，覆盖“收到回答后、图恢复前崩溃”的窗口。

回答以 `user_supplied_unverified` 进入下一次模型上下文，不添加到 Kubernetes evidence。跳过问题直接交接，不自动发起新模型请求。已消费的回答再次提交返回当前状态，不重复调用模型。

等待不持续运行图，也不持续扣活动时间；查询等待状态直接读取 checkpoint。恢复时继续使用同一 run 的预算。长时间等待后，旧采样不足以证明当前健康：本块禁止用回答后的旧 baseline 输出 no_fault_detected，需要未知/范围受限的结论或新一轮完整基线。此限制不宣称已经实现完整资源刷新器。

### 3. 怎样判断复采合理

大白话：先看是不是同一个问题。同一个问题只有“用户明确确认对应对象发生变更”或“确实过了规定时间”才能再查；模型自己说“可能变了”不算依据。

`resampling.py::authorize_sample` 在预算行锁内查已完成采样，为符合条件的新请求保存采样授权。模型只能提交 `resample_reason=user_change/stale`，不能指定采样代次、计时或授权编号。

| 场景 | 程序条件 |
| --- | --- |
| 首次查询 | 无历史同语义调用，代次 0；不接受伪装为复采的首次请求 |
| 用户变更 | 有已保存的 changes 回答，用户明确勾选/提交本次查询的 resource_ref，接收时间晚于旧采样；只支持该对象 |
| 时效到期 | 使用服务端 UTC 时间与旧结果 collected_at 比较，不采用模型自报时间 |
| 未完成/结果丢失 | 交接未知，不通过复采绕过在途请求 |
| 权限失败、未知采样结果 | 本块要求新一轮调查，不能用“过期”重复冲击拒绝请求 |
| previous 日志 | 历史日志不因等待变成新证据，本块不自动复采 |

时效初值：资源概要/登记业务/EndpointSlice 为 30 秒，当前日志/事件/Deployment/ReplicaSet 为 60 秒。它们是可审查的固定策略值，不是已实测的最佳参数；本块不开放环境变量调优。

查询指纹仍按工具、namespace、UID、容器和 previous 区分，改日志行数或无关证据不算新查询。授权存于 `sampling_grants`，包含 semantic_key、generation、basis 和新 query_key。同一用户变更依据对同一查询仅能授权一次；恢复按 request_id 复用原授权，不重新判断一次时效后重新生成编号。

整批采样授权检查完才执行第一项。工具层核对授权与账本一致，然后仍执行原预算预留、读取前后 UID/归属/profile 检查和结果保存；有效复采授权不能绕过预算或对象替换。旧观察保留在 observations，当前视图使用新观察；复采失败仍使对应旧证据失效。

**接口边界：** `changed_resource_refs` 是明确的用户确认字段，本块没有通过 LLM 从“我改了配置”自动推断对象。尚未接前端选择控件或现有回答 API；这一步属于 6B-2b，不能将内部回答函数当成身份认证接口。

### 4. 版本和兼容

新图版本为 `interactive-investigation-v2`，原图仍为 `readonly-investigation-v1`。`bind_baseline` 同时绑定版本与基线摘要，恢复入口和各执行节点再次校验版本。旧 checkpoint 不会被直接加载为新图继续运行。正式 worker 按数据库 workflow_version 分流将在 6B-2b 实施，本块没有新增默认开关。

## ECS 验收与变更要求

在 ECS 项目目录、Python 3.12 `.venv` 已激活时执行：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6b2a.sh
```

预期全部通过、零 skipped，末尾输出 `PASS: 6B-2a ECS acceptance.`。脚本包含 6B-1 回归、原对话控制测试及新增人工等待/复采测试。真实 PostgreSQL 存储和 checkpoint，模型/集群响应受控，不调用线上模型或修改真实集群。

证据目录：`evals/results/6b2a/incident_agent_test_6b2a_.../`。保留 `backend.txt/junit.xml/evidence-change.json/human-resampling.json/commit.txt` 和隔离测试数据库。`human-resampling.json` 展示“第一次日志 → 追问变更 → 人工确认对象 → 第二次日志”的问题、回答、采样代次和调用次数；不证明真实模型质量。

新增模块：`investigation/dialogue.py`、`resampling.py`；扩展 `contracts/context/graph/records.py` 和 `tools/investigation.py`；新增 `tests/investigation_dialogue/test_dialogue.py`，复用验收运行器。

不新增依赖、环境配置或数据库迁移（仍复用迁移 13）；不要求重启 backend/worker/frontend，不构建前端。当前仅本地静态检查通过，ECS 动态结果待反馈。

尚未验证或实施：真实模型追问质量、时效定标、浏览器交互、现有消息/API 权限与内部回答适配、正式 worker 挂起/唤醒/发布、确定性计划和审批衔接。下一块接这些业务边界，6B-3 再展示调查过程并作真实模型对照。
