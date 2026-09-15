# 代码阅读指南

本文描述恢复闭环与只读复查阶段存档。路径均相对于仓库根目录；历史 day 文档保留当时设计，当前行为以源码为准。

## 按一次事件阅读

1. `backend/app/agent/state.py`、`graph.py`：共享状态、节点、失败路由、审批中断与恢复。先理解控制流，再读较长的节点实现。
2. `backend/app/agent/dependencies.py`、`collector_adapter.py`：生产依赖装配，工具结果转换为 Evidence。
3. `backend/app/service_profiles/models.py`、`registry.py`：登记配置、摘要兼容、版本匹配、现场快照与执行前重新核对。
4. `backend/app/business_checks/collector.py`、`server.py`、`protocol.py`：后端请求检查器、检查器访问登记 Service、结果与登记目标重新绑定。
5. `backend/app/agent/diagnosis_policy.py`：从 Evidence 计算事实，再检查诊断是否相容。留意“证据缺失”和“明确不存在”的区别，以及探针配置漂移与可修复性的区别。
6. `backend/app/llm/context_builder.py`、`backend/app/agent/nodes.py`、`diagnosis_report.py`：动态输出契约、模型调用、校验反馈、有限重试与正式报告。`backend/app/llm/debug_capture.py` 保存调用审计。
7. `backend/app/agent/remediation_policy.py`、`approval.py`、`execution_policy.py`、`executor.py`：允许动作、审批快照、执行资格及现场复核。真正的固定 PATCH 在 `backend/app/tools/remediation_tools.py`；旧值/resourceVersion 冲突保护不能被审批前检查替代。
8. `backend/app/agent/verification.py`、`business_recovery.py`：先轮询资源，再重新采集登记业务；后者提供退出 Pod 的判定视图。不得修改原始 Evidence 来隐藏不健康状态。
9. `backend/app/services/incident_service.py`、`backend/app/persistence/`：事件投影、检查点和审批恢复。数据库记录持久化不等于业务恢复或执行 exactly-once。

## 人工复查是独立路径

`backend/app/api/routes/rechecks.py` → `backend/app/services/recheck_service.py` → 现有 collector/facts → `backend/app/persistence/rechecks.py`。

- `RecheckRequest` 限制请求输入；服务检查原事件是否允许复查。
- 新采集和新判定不调用模型、不恢复工作流、不改 Kubernetes。
- `compare_targets` 只比较已有明确字段，不把局部比较描述为完整对象身份一致。
- 仓储独立 INSERT，事务完成后才返回成功；历史按数据库 sequence 分页，不读改写原事件的数组。
- `backend/app/persistence/migrations.py` 通过迁移 2 新增表；`scripts/recheck_incident.py` 提供调用、历史和原事件未变检查。
- Web 尚未接入此路径，GET 原事件不会自动附带复查历史。

## 字段语义

| 字段或标识 | 含义和限制 |
| --- | --- |
| `service_profile.status=matched` | 登记与现场声明匹配，不代表健康，也不是镜像可信证明 |
| `assessment.resource_status / business_status` | 原诊断采集时的两类状态，不随人工复查改写 |
| `readiness_configuration_status` | matched/drift/unknown；不同于探针请求是否成功 |
| `readiness_patch_supported` | 当前证据是否支持已有受控工具修复，不能独立代替计划及审批校验 |
| `diagnosis_model_output` | 模型结构化原始输出，不是最终受控报告 |
| `llm_debug` | 服务层调用审计，不保证覆盖底层 SDK 每次传输重试 |
| `verification_result` | 原处置后的恢复验证结果；资源等待通过后包括重新采集的登记业务检查 |
| `post_repair_evidence / unverified_scope` | 处置后原始证据和未覆盖范围；不能从通过推导长期稳定 |
| 复查 `status` | 当前独立观察的 passed/failed/unknown，不改变旧事件 phase |
| `same_observed_fields` | 摘要、Deployment UID/generation 等指定字段相同，不代表所有资源相同 |
| `recovery_attribution=not_established` | 复查不认定恢复由 Agent 导致 |
| `rule_precheck` | 本次诊断由规则生成；不证明上游检索无 Embedding 开销 |
| `llm_with_controlled_report` | 已实现范围内用事实生成正式说明，不表示全部正文均受控 |

## 校验合并时的约束

可优先聚合同一次模型输出中能独立判断的 Schema、引用和事实错误，统一反馈；依赖前一步成功的检查仍需分层执行。当前聚合尚未完整实现。

不能合并掉审批后的现场复核、工具写入时的冲突检查、写入后的恢复观察。它们面对不同时间点和信任边界。计划允许写入，不代表现场仍未变化；写入成功，不代表资源或业务已恢复。

## 测试与保留目录

先读 `scripts/run_fault_case.py` 与 `scripts/fault_cases/suite.py`，最后读 `operator.py`，避免将操作者注入故障的权限误认为 Agent 能力。合成固定响应、live facts、真实模型和真实写入是四种不同验收范围。

`backend/app/diagnosis/` 仍有规则引擎 CLI 与测试用途，后续可作为对比基线的起点，尚不是公平完整的业务诊断基准。历史 profile、smoke 入口、迁移与阶段文档不因“旧”就可删除。

本次只对五个近期核心 Python 文件补充解释性注释并局部格式化，保持 AST 一致；没有全仓重排、删除兼容入口或改变业务行为。
