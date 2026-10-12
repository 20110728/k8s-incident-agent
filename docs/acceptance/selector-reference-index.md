# Selector 修复候选被引用索引拦截

2026-10-12 导出显示：两次决定都是 `propose_plan`。第二次选中 `patch_service_selector`，正文引用已展示的 Pod 证据 006、007，却漏填顶层 `evidence_ids`。不是 selector 被规定只能人工排查，也不是修复已执行。第一次还未通过诊断完整性检查；现有导出不足以还原第一次完整 assessment。

## 本次范围

- 多轮复用旧单轮的精确引用索引补齐，只接受本次可见且采集成功的证据。不增加假设依据，不推断根因，不自动填缺失调查建议。
- 补齐后仍执行诊断语义、修复权限、身份和审批检查。虚构、不可见或失败的缺漏引用继续拒绝。
- 模型原始输出保留；接受的补齐记录在 `reference_normalization`，完整、brief、last-llm 导出均可查看。
- 这是第三块前的窄修复，不代表 unknown 报告改进已完成。

## ECS 验收

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6bc2.sh
# 等活动调查结束后，更新 backend / worker
bash scripts/deploy_stage6b2b_backend.sh
bash scripts/stage5_fault.sh reset
bash scripts/stage5_fault.sh selector_mismatch
```

在页面新建 order-service 事件。旧拒绝记录不会被重写或自动恢复。

验收分开记录：
1. 自动回归：已有真实 PostgreSQL / worker 验收覆盖新测试；只漏索引时不额外纠正，等待审批前零写入；批准后一次写入并复查，拒绝后零写入，重放不重复执行。
2. 实际模型：输入含真实 Service selector、登记配置和工作负载证据；报告指出配置差异，有依据的 supported 假设引用 Service 与 Deployment；进入等待审批，而不是以手工操作代替计划。若模型原本完整填写引用，无补齐记录也是正常。
3. 页面批准后确认执行与资源、业务复查结果，不能只看脚本 PASS 或工作流 succeeded。无法证明恢复时不得宣称已恢复。
4. 如仍拒绝，导出 brief / last-llm 检查新的具体原因；本修复不保证任何模型输出都能通过。

无新增依赖、配置或数据库迁移，无需重建前端或 Demo 镜像。本地仅做 Python AST 与 diff 静态检查；动态回归、真实模型及集群执行留给 ECS。
