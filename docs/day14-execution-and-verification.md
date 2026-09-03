# Day 14：白名单处置执行与恢复验证

## 完成内容

Day 14 在人工审批之后增加了受控 Kubernetes 写操作和恢复验证。

当前允许执行的动作只有：

- `patch_readiness_probe`
- `patch_service_selector`

其他故障类型继续使用 `manual_investigation`，不会自动修改集群。

## 工作流

完整工作流为：

`validate_request`
→ `plan_collection`
→ `collect_evidence`
→ `retrieve_runbooks`
→ `diagnose_incident`
→ `plan_remediation`
→ `request_human_approval`
→ `execute_remediation`
→ `verify_recovery`

## 安全边界

- 只允许 `agent-demo` 命名空间。
- 只允许修改 `order-service`。
- 只允许 Patch Service Selector 或 Deployment Readiness Probe。
- 不允许 LLM 生成或执行 Shell 命令。
- 写入参数必须来自 Kubernetes Evidence。
- 所有可执行动作必须经过人工审批。
- 审批记录必须与处置方案绑定。
- 执行前重新读取资源并检查当前值。
- 使用 `resourceVersion` 防止覆盖并发修改。
- 已成功执行的动作不会重复执行。
- 执行失败后不能沿用旧审批重试。
- 回滚补丁只记录，不自动执行。
- 写入后必须验证 Deployment、Pod 和 EndpointSlice。

## 执行状态

- `succeeded`
- `already_applied`
- `conflict`
- `failed`

## 恢复验证状态

- `succeeded`
- `failed`
- `timeout`
- `skipped`

## RBAC

ServiceAccount `incident-agent` 只能：

- 读取诊断所需资源；
- Patch `agent-demo/order-service` Service；
- Patch `agent-demo/order-service` Deployment。

它不能删除资源、创建资源、修改其他命名空间或修改其他工作负载。

## 回滚策略

系统保存修改前快照和回滚补丁，但 Day 14 不执行自动回滚。

如果恢复验证失败，系统返回结构化失败结果，由操作者检查后决定是否在新的审批流程中执行回滚。