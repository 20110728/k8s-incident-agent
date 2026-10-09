# 6B-3a：看清调查过程

状态：6B-2b 脚本及容器开关验证已由维护者反馈通过。本块已实现待 ECS 验收。6B-3 分为 3a 展示/资源确认、3b 真实模型小规模对照，每次仅验收一块。

## 核心机制

- **调查过程**：`investigation/presentation.py::investigation_view` 从已保存的 checkpoint/历史快照投影；不执行图、不调用模型。通过 `api/schemas.py` 的可选 `investigation` 字段返回至页面；旧图返回 null。只取行动、简短理由、缺失事实、工具覆盖/错误、采样代次和证据编号，脱敏后限长，不返回原始 prompt、工具全文或内部授权记录。最多 4 步/64 条采样，超出数量明确提示。
- **新旧证据**：初始证据和追加观察均保留摘要；是否“当前”按本轮 evidence 视图的编号判断。`InvestigationPanel.tsx` 用独立环节展示，历史证据不生成无效跳转，当前引用跳到“采集与证据”。没有整批完成记录就显示未完成；失败/截断不渲染成健康，建议修复不渲染成已执行。
- **明确变更对象**：`IncidentWorkbench.tsx::QuestionForm` 仅在 changes 问题下展示服务端候选对象，默认不选、最多 20 项；提交 `changed_resource_refs`，跳过时清空。问题编号/版本改变会重置表单。保留原消息编号与请求体用于不确定请求恢复；服务端仍按 6B-2b 检查候选范围和幂等。勾选不等于一定复采，更不等于写授权。
- **调用开销**：预算 API 从持久化调用账本统计生成请求尝试、供应商已报告 token、未报告用量次数；含失败/中断，不声称每个预留都到达供应商；Embedding 不混入生成调用数。页面保留预算总记账和估算说明，不将缺失 usage 当零，也不新增总结模型。预算 API 仅返回调用公开字段，内部缓存的 baseline/日志/模型结果和指纹不再随预算接口返回，数据库原记录不变。

主要文件：上述模块、`runtime/budget.py`、`api/types.ts`、`RunBudgetPanel.tsx`、导航/CSS；新增后端投影/API测试、前端渲染/请求恢复测试和两份脚本。

## ECS 顺序

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6b3a.sh
# 看到 PASS 后再部署：
bash scripts/deploy_stage6b3a.sh
```

预期末尾 `PASS: 6B-3a ECS acceptance.`，后端零 skipped；前端测试和 TypeScript/Vite 构建通过。脚本使用隔离数据库，模型/集群响应受控；不调用真实模型。日志位于 `evals/results/6b3a/incident_agent_test_6b3a_.../backend.txt`、`frontend.txt`、`junit.xml`，保留测试库及原有调查报告。

配置沿用 queued + investigation_enabled=true；不新增配置、依赖或迁移。部署脚本复用既有命令重建 backend/worker 容器、构建并更新 frontend，分别检查 readyz 和 frontend-healthz。

浏览器只核对本块：
1. 打开已有新图事件，点击“调查过程”：看到已保存行动、采样摘要、停止原因和预算；刷新不会追加步骤或调用。运行中可能暂时没有记录。
2. 切换历史轮次：展示对应轮次，旧图显示“没有调查过程记录”，不把当前轮次的内容套给旧轮次。
3. 当前证据引用可跳转；失效旧引用标历史。只有受控失败/复采场景才必然出现旧证据，真实健康事件不应强行制造多次调用。
4. 若遇到 changes 追问：对象默认不勾选；确认对象后回答，或选择跳过。已无此追问则该项记未手测，脚本覆盖适配和边界，不为了展示强迫模型提问。

本地通过 Python AST（249 文件）、TypeScript 6.0.3 的 TS/TSX 语法解析（34 文件）、两份 Bash 脚本语法及 Git 差异检查；未运行 pytest、前端测试或构建。完整 TypeScript 类型检查随 ECS 构建执行，实际浏览器样式/交互尚待反馈。真实模型结构化输出、补证改判质量及与固定流程的耗时/调用对照留给 6B-3b，本块不宣称已验证。

## 校验失败排障补丁

`diagnostics.py::record_validation` 在原预算行锁内按 request_id 保存拒绝阶段（解析/字段结构/策略）、尝试次数、动作及脱敏限长原因，同时保存当次允许引用的证据/资源编号用于导出。重放复用原记录，不追加模型调用、不放宽校验；成功纠错前的失败记录也保留在预算账本。图终止时将最后一步的拒绝摘要写入 output，页面直接展示；未通过的决定不计入已执行行动。

`model.py` 额外保存供应商 finish_reason、解析错误类型；解析失败时保存最多 3000 字符的脱敏最终输出片段，不保存隐藏推理、完整 prompt 或认证头。旧记录未保存的内容无法补回，页面明确提示。请求异常仅保存异常类名，不输出可能包含凭证的异常全文。

只读导出（项目目录、`.venv` 已激活，不必重启或重跑旧事件）：

```bash
python -m scripts.export_investigation_debug --incident-id <事件ID>
# 指定历史轮次时再加：--run-id <runID>
```

命令在数据库只读、可重复读事务中读取该事件最新 diagnosis run（或指定 run）的现存账本；不恢复 graph、不调用模型/集群。生成 `evals/results/investigation-debug/<时间>-<随机ID>.json`，含所选 run ID、解析结果、供应商诊断、校验记录及引用范围，不导出完整采样日志/数据库连接串。发送前仍可检查模型自由文本是否含需自行隐藏的业务信息。旧事件导出后先分析文件，勿为补日志反复请求模型。

补丁沿用 `bash scripts/accept_stage6b3a.sh` 验收，包含失败原因持久化/缓存重放、脱敏、旧记录导出及真实 PostgreSQL CLI 测试；通过后沿用 `bash scripts/deploy_stage6b3a.sh`。无新配置、依赖或迁移；根本触发原因须结合实际导出文件确认。

## 工具参数契约修复

旧事件 `d523afa6-f382-463f-82ec-0438bd26a92d` 已保存的 `pod_events(tail_lines=50)` 暴露了契约矛盾：通用 schema 允许行数，但执行层将非日志工具的非默认参数归入权限错误，导致直接交接。旧记录缺少校验异常，不能断言这是当时最先触发的检查。

- `tools/investigation_requests.py` 统一模型与执行层参数定义：日志分支允许 `previous/tail_lines`；其他工具分支仅允许工具名和资源编号。只有旧的精确默认值 `false/100` 可兼容，非默认值、隐式类型转换和未知字段均不忽略。执行层继续生成原字段顺序和默认值，保持工具请求指纹、去重键与缓存兼容。
- `graph.validate_decision` 对已解析采集批次先检查资源/工具边界，再做完整字段和证据校验，整批通过才允许采集。明确的 `ToolBoundaryError` 立即停止，不再依靠错误字符串包含 `RESOURCE_` 判断权限；普通参数错误复用全轮共享的一次纠错，不提高调用预算。结构化解析失败若能取得 Pydantic 错误，则提供限长字段路径；识别出的非法工具标签/资源编号格式立即停止。无法解析的响应不执行工具，最多使用原有纠错额度。
- `diagnostics.provider_diagnostics` 从有限层异常链提取字段错误，不解析异常全文；页面与导出沿用上一补丁。工具运行中的身份变化、预算限制及部分失败处理不变，已成功结果保留，未完成项不能算成功。
- 升级边界：已完成旧事件不重跑；旧工具结果可按原请求 ID 复用。模型上下文/schema 已变化，升级前应结束正在调查的任务；若旧的未完成模型节点重放时上下文指纹不同，仍以 `REQUEST_INPUT_CHANGED` 保守停止，不能跳过指纹检查或自动重付费调用。需要时明确创建新调查轮次。

新增 `investigation_loop/test_request_contracts.py` 覆盖本次错误的纠正/再次失败、整批检查、权限拒绝、正常路径无额外调用、部分成功保留、旧工具指纹缓存、模型上下文变化保守停止，以及真实结构化解析错误的适配路径（受控供应商，真实隔离 PostgreSQL）。本地只做静态检查，动态结果由 ECS 验收确认。

本补丁本地检查：`backend/` 与 `scripts/` 下 251 个 Python 文件 AST 解析、Git 差异检查通过；新增 11 个测试函数（含参数化用例），未本地运行 pytest、模型调用或前端构建。

ECS 顺序：`git pull --ff-only origin feature/baseline-contracts` → `bash scripts/accept_stage6b3a.sh`（预期 `PASS: 6B-3a ECS acceptance.`）→ `bash scripts/deploy_stage6b3a.sh`。无新增配置、依赖或迁移；部署更新 backend、worker、frontend。最后用新事件复验浏览器调查，真实模型可选择不同有效路径，不要求必定调用日志和事件；遇到失败导出该新事件，勿重跑旧事件补记录。

ECS 首次复验为 351 passed / 1 failed：诊断测试将缺少 `get_round` 的 worker 仓储传给预算展示函数。测试已改用与 API 相同的 `InteractionRepository`，连接原隔离测试数据库，并断言其他事件不能读取该任务预算。业务代码和权限检查不变；需重跑上述验收脚本确认，Starlette 弃用警告不是本次失败原因。

## 新采样进入上下文与重复调查修复

事件 `1afeacf6-da22-4c58-b2c5-a8d5a602a325` 两次提出相同的两个当前日志查询，最终因 `RESAMPLE_REASON_REQUIRED` 停止。旧导出未保存输入证据清单，不能证明本次实际遗漏了哪些证据；代码确认存在基线大段内容优先挤占新日志空间的缺陷。

- `context.build_context` 先给最新批次、其他调查采样、基线证据分配短片段，再用剩余空间扩展，保留截断标识；日志正文排在其元数据前。最近批次仍有效的证据若无法装入，明确停止而非继续付费请求模型。输入上限仍为 8000，遗漏清单从开始计入预算，资源与 runbook 保留原有限额。
- `graph` 保存行动的具体 requests。交互/生产图对已采样查询缺少复采理由的请求，在执行前使用原有共享纠错额度反馈；失败仍停止。真正复采继续由 `authorize_sample` 检查变更回执、时效、历史日志限制和预算，不自动补理由、改参数或提高调用上限。
- 模型账本 metadata 保存实际送入的证据编号/覆盖/错误/截断标识及遗漏编号；只读导出额外列出最多 6 次工具请求与采样状态，不导出完整日志或 prompt。旧采样状态可导出，旧模型输入清单不能补回。
- `test_context_feedback.py` 覆盖大基线与两份长日志共存、下一轮确实收到两份结果、缓存重放、重复请求有限纠错且不重复读取，以及空间不足明确停止。真实模型是否有效修正判断仍须新事件实测。

复验、部署继续使用上述两个 6B-3a 脚本；无新配置、依赖、迁移。模型上下文指纹发生变化，沿用先结束活动调查再部署的升级要求。仅本地静态检查，不宣称动态用例或真实模型已通过。

后续 ECS 复验为 354 passed / 3 failed：新增交互回归用例误用无 checkpointer 的只读测试入口，触发 `INTERACTIVE_CHECKPOINTER_REQUIRED`。已改用现有 PostgreSQL `session` 测试会话，恢复用例重新打开同一线程的持久化 checkpoint；生产保护不变。现有 backend.txt 足够定位，需重新验收，不需新增配置或迁移。

## 上下文压缩与 6 万预算

- 新预算快照为 `investigation-budget-v2` / 60000 Token；已有账本不修改原上限。调查仍最多 3 轮、6 次追加工具、5 次模型尝试，单次输入估算 8000 / 输出 1500 不变。
- `compact.py` 按资源类型选取诊断字段，日志按相同消息合并并保留次数与首尾示例；保留有限组并明确不完整。`context.py` 必须保留配置/资源/业务关键证据和所有当前调查采样的短片段，空间不足明确停止，不悄悄丢掉旧采样。历史理由限长、保留 requests/results；Runbook 最多 2 条各 350 字符。完整原证据不改写；不调用摘要模型。
- `model.terminal_mode` 在每次调用（含纠错）前检查账本；不足两次调用额度/时间，或已用 4 次尝试，则转为仅 conclude/stop 的收尾模式。模式按 request ID 持久化，恢复不能因余额变化改变已付费调用的 prompt。收尾不再预留下一次调用；一次也付不起则停止，权限、证据验证及总调用上限不变。
- 新调查的检索查询只取目标、描述和关键症状，限制 2400 UTF-8 字节，因此当前保守 Embedding 估算不超过 2912；通用旧流程检索入口不变。预算 API/前端分别展示已报告用量对应记账、估算/预留、Embedding 分项和余额，明确不是精确费用。
- 新增 `test_compact_budget.py` 使用真实隔离 PostgreSQL/checkpointer，覆盖新旧额度、原事件的 17391 余额能收尾、纠错转收尾、禁止收尾采集、单次余额不足、已采样证据保留与检索压缩。沿用 `accept_stage6b3a.sh` / `deploy_stage6b3a.sh`，无新增配置、依赖或迁移。先结束活动任务再部署 backend/worker/frontend，用新事件确认总额 60000；旧事件仍显示原额度。

本补丁只做本地静态检查；ECS 动态测试、供应商实际 Token 降幅及真实故障诊断效果仍需验收，不将受控模型通过等同于真实模型通过。
