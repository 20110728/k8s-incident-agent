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
