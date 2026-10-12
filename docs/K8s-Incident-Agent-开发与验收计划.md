# K8s Incident Agent：开发与验收记录

**状态：维护者于 2026-10-12 确认本轮连续阶段开发与验收收尾。** 本文沿用原文件名，内容由开发路线图整理为实际交付记录，不再保留排期、拟建架构或未来功能承诺。

分支：`feature/baseline-contracts`。功能实现截至 `c01615c`，后续为文档整理；本次收尾不修改包/API 版本。验收结论来自维护者 ECS 验收确认及历史分块记录，本文不新增测试次数或效果统计。

## 1. 本轮交付范围

| 范围 | 已实现内容 | 主要入口（backend/app 下） |
| --- | --- | --- |
| 异步任务 | 创建时持久化事件与 run，请求幂等，worker 领取、心跳与租约 | `persistence/runs.py`、`runtime/worker.py` |
| 中断恢复 | 检查点续跑，等待用户/审批保留，失去租约后限制旧执行者推进 | `runtime/recovery.py`、`persistence/leases.py` |
| 外部写审计 | 写前保存操作意图、计划与审批；保存响应与核对结果；不盲目重放未知结果 | `runtime/operations.py`、`persistence/operations.py` |
| 目标与授权 | 登记配置、资源 UID/版本、审批绑定、执行前现场复核及受限写动作 | `agent/target_identity.py`、`agent/execution_policy.py` |
| 多轮交互 | 同事件消息、追问、补充、新调查轮次、停止及历史查看 | `persistence/controls.py`、`investigation/dialogue.py` |
| 多轮调查 | 基线与检索、按需补采、模型决策、结构化诊断与程序生成修复计划 | `investigation/graph.py`、`investigation/production.py` |
| 恢复观察 | 资源与登记业务连续采样、单次复查、稳定性观察、持久化延时复查 | `agent/observation.py`、`runtime/delayed_rechecks.py` |
| 可观测性 | 证据预览/正文分离、用量和拒绝原因记录、模型及恢复独立导出 | `investigation/exchange.py`、`runtime/remediation_export.py` |

LangGraph 负责状态、检查点与暂停恢复；应用层负责持久化任务、租约、授权及外部副作用核对。incident 保存长期事件，run 保存一次任务，operation 保存一次授权写入；工作流完成不等于业务恢复。

## 2. 证据到报告的改进

- Demo 记录真实依赖请求失败及其原因，不将故障注入标签作为诊断答案。
- 日志采集保留最近 1000 行、单份 256 KiB 上限；原始证据、模型表示、前端预览与排障导出各自分离。
- 模型输入使用原生 JSON；精确重复探针日志按次数、时间和来源行整理，非探针日志保留原文，不增加模型摘要调用。
- 对模型正文已引用、本次可见且采集成功的证据，只补齐顶层引用索引，不替根因假设补造论据。
- 配置漂移与 supported 假设引用检查先于调查字段豁免，纠正反馈包含具体字段和缺失编号。
- 纯诊断语义失败的既有保守兜底不生成写计划；引用、身份、权限或存储失败不能借兜底绕过。

对应记录：[应用证据](acceptance/evidence-report-1.md)、[模型输入](acceptance/evidence-report-2.md)、[引用校验](acceptance/selector-reference-index.md)、[恢复导出](acceptance/remediation-export.md)。完整 unknown 报告规范化及检索排序优化未纳入本轮交付，不列为未完成的开工任务。

## 3. 当前契约

- 每次调查最多 5 轮决策、6 次模型调用，追问占决策轮次，失败与纠正计入调用次数。
- 不设置累计 Token、总时间或工具总次数额度；保留用量记账、单次网络超时与独立恢复窗口。
- 写操作仅限登记 Service selector，以及登记 Deployment 指定容器 readiness HTTP path/port。参数由程序生成，必须人工审批。
- 用户陈述是线索，不代替现场证据；历史状态不自动当作当前故障，旧审批不授权变更后的计划或目标。
- 恢复默认连续 3 次登记资源与业务采样通过；保存未验证范围，不从成功响应推导业务恢复。
- 原单轮、历史事件与旧复查入口保留兼容，不删除共享回归或迁移记录。

## 4. 验收依据与使用方法

维护者已宣布本轮开发验收收尾。此前分块记录覆盖持久化、进程恢复、权限、交互与调查；实际案例讨论覆盖 dependency 证据诊断、selector 受控修复及恢复观察，最终 readiness 验收按维护者本次收尾确认记录。本文不据此推导所有场景均稳定，也不补写未提供的 readiness 原始计数或导出内容。

| 验收类型 | 证明什么 |
| --- | --- |
| 程序回归、真实 PostgreSQL 集成 | 字段契约、状态流转、任务与审批持久化 |
| 进程恢复与并发验收 | 指定中断窗口下的租约、续跑及操作核对 |
| 受限身份与集群验收 | 登记资源读写边界、现场冲突与实际恢复 |
| 真实模型及页面闭环 | 典型场景的有效取证、诊断、审批与复查 |

以上层级分别保留，脚本 PASS 不替代真实诊断有效性。没有公布公平基线对照或统计意义上的准确率、成本收益数据。

常用 ECS 命令（项目根目录，Python 3.12 虚拟环境）：

```bash
bash scripts/accept_stage6bc2.sh
python -m scripts.export_investigation_debug --incident-id YOUR_INCIDENT_ID --brief
python -m scripts.export_investigation_debug --incident-id YOUR_INCIDENT_ID --last-llm
python -m scripts.export_remediation_debug --incident-id YOUR_INCIDENT_ID
```

需要复核历史事件时用 `--run-id` 指定轮次。导出只读数据库，不重放执行。6B-C2 脚本使用受控模型/集群与真实隔离 PostgreSQL，不替代所有早期恢复和权限验收；原 `scripts/accept_stage*.sh` 按修改范围使用。

## 5. 维护与边界

本地只做静态检查，动态测试、数据库、真实模型和集群验收在 ECS 执行。之后每次修改仍按小块提交，说明涉及文件、静态结果、ECS 命令及预期、部署要求和未验证内容。

后端或模型配置变更需同步更新 backend/worker；文档或本地导出 CLI 变更无需重启。已接受任务和未知写结果不能通过删除数据库或检查点解决。

当前为受控单人项目：没有完整认证、多租户或跨集群能力，不开放任意执行，也不承诺跨数据库和 Kubernetes 的 exactly-once。自然语言解释仍可能过度推断；程序校验不等于全面证明每句话正确。恢复只覆盖登记采样范围，现场修复后配置仓库同步由负责人处理。

历史分块文档和案例规格用于追溯，不再作为必须逐项实现的未来路线图。操作与启动说明统一见 [README](../README.md)，代码导航见 [CODE_READING](CODE_READING.md)。
