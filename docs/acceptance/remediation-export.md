# 独立恢复审计导出

`python -m scripts.export_remediation_debug --incident-id <事件ID> [--run-id <调查轮次ID>]`

输出到 `evals/results/remediation-debug/*-remediation.json`，与诊断及模型导出分开。默认选择事件最新诊断轮次，即使该轮没有恢复计划；导出旧轮必须指定 run-id，不能自动借用其他轮次的执行记录。

内容：已发布计划、审批请求和决定、执行结果、持久化 operation（实际 patch、目标身份、执行前/目标/响应/对账快照和时间）、完整已保存复查结果及范围、恢复环节 trace/errors。保留原始状态与空值，经过现有脱敏处理；不包含诊断正文、模型输入或调查原始日志。执行审计不等于容器 stdout 或完整 Kubernetes 审计日志；未保存的数据无法补造。运行中快照可能落后于 operation。

## ECS 验收

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6bc2.sh
python -m scripts.export_remediation_debug --incident-id c90ad318-f04a-404d-b6af-f5878d964813
```

预期：自动回归覆盖批准/拒绝、仅已保存数据读取、跨事件轮次隔离、脱敏、未知执行状态保持。实际事件核对 selector 计划与批准参数一致，operation 的 patch/结果对应同一 run，verification 为资源 ready、登记业务 passed、连续观察 3/3；不能把 run succeeded 当作恢复证据。若最新轮次不是已执行轮次，使用该次实际 run-id 重导出。

本功能只新增本地 CLI 与只读投影：现有 ECS venv 配置下 git pull 后即可导出旧记录，无需重启 backend/worker，无新增配置、依赖或迁移。若选择在容器内执行，需先更新容器代码；本说明使用 ECS 项目 venv。

本地只检查 AST 和 diff；数据库及真实事件导出由 ECS 验证。selector 的复查计数中 desired_replicas/available_replicas/ready_pods 为 null 是当前检查路径设计，不表示失败；它直接检查批准 selector 与 Ready endpoints，业务恢复另由登记接口复查确认。
