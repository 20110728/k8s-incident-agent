# 代码阅读指南

## 建议阅读顺序

1. `backend/app/agent/state.py`：认识共享状态和追加型 trace/errors。
2. `backend/app/agent/graph.py`：理解节点顺序、失败路由、审批中断与恢复。
3. `backend/app/agent/dependencies.py` 与 `collector_adapter.py`：理解生产能力如何装配、采集结果如何转成 Evidence。
4. `backend/app/service_profiles/models.py` 与 `registry.py`：理解登记配置、应用版本和现场快照的关系。
5. `backend/app/business_checks/collector.py` → `server.py` → `protocol.py`：跟踪后端请求检查器、检查器访问 Service、结果重新绑定的完整路径。
6. `backend/app/agent/diagnosis_policy.py` → `nodes.py` → `diagnosis_report.py`：区分事实计算、模型调用、引用校验、本地预判和正式报告生成。
7. `backend/app/agent/remediation_policy.py` → `approval.py` → `execution_policy.py` → `executor.py`：理解允许动作、审批快照、幂等与现场复核。实际资源修改在 `backend/app/tools/remediation_tools.py`。
8. `backend/app/agent/verification.py`：了解当前资源验证范围，避免把它当成已完成的处置后业务验证。
9. `backend/app/services/incident_service.py` 与 `backend/app/persistence/`：跟踪事件状态、检查点与审批恢复。
10. `scripts/run_fault_case.py` 与 `scripts/fault_cases/suite.py`：区分输入、预期答案和实际输出；最后阅读 `operator.py` 了解操作者故障注入边界。

## 容易误读的字段

| 字段或标识 | 正确含义 |
| --- | --- |
| `service_profile.status=matched` | 登记配置与当前应用声明、资源关联匹配，不表示服务健康 |
| `assessment.resource_status` | 采集时的资源状态 |
| `assessment.business_status` | 登记业务检查的聚合状态 |
| `diagnosis_model_output` | 模型原始结构的审计副本，不是正式受控报告 |
| `verification_result` | 当前仅覆盖处置后的资源验证 |
| `rule_precheck` | 本地规则直接构建诊断，本次未调用诊断模型 |
| `llm_with_controlled_report` | 模型分类通过校验，正式说明由结构化事实生成 |
| `source=replay, mode=regression` | 合成输入与固定响应的程序回归，不是模型基准成绩 |
| `source=live, mode=facts` | 真实采集的事实断言，不是完整 Agent 处置验收 |

## 注释约定

本轮使用 `#` 文件头与行间说明，保留已有 docstring 和 `__future__` 导入语义。注释解释职责、信任边界与关键前置条件，不逐行复述赋值。未修改函数签名、状态字段、模型提示词、审批或持久化逻辑。

注释覆盖本轮核心链路与 Demo 共 22 个 Python 文件；前端、底层采集工具及历史测试的现有注释继续保留，未进行全仓格式化或机械添加注释。
