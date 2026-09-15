# K8s Incident Agent

面向将业务部署到 Kubernetes 后需要排障的开发者，提供可追溯的诊断、人工审批后的受控修复，以及资源和登记业务接口的恢复检查。

项目采用 **预定义 LangGraph Workflow**：模型负责解释证据和提出结构化诊断、计划，程序负责校验事实、限制动作和控制执行。目标是在有限场景中完成可审计的排障闭环，并明确证据不足时不能得出的结论。

**当前状态：v0.2 阶段存档（恢复闭环与人工处理后只读复查）。** selector、readiness 的真实修复及 Recovery verification 已获维护者验收；人工调查事件复查、后端重启后复查记录仍存在也已获验收。页面复查交互、真实受限 Kubernetes 身份验收和同批案例的模型指标对比尚未完成。Python 包/API 版本仍为 `0.1.0`，本存档不标记完整 `v0.2.0` 发布。

## 项目概览

| 项目 | 内容 |
| --- | --- |
| 使用场景 | Kubernetes 应用发布、配置漂移及运行异常的辅助排查 |
| 技术栈 | Python / FastAPI、LangGraph、PostgreSQL / pgvector、React / TypeScript |
| 开发与演示环境 | 阿里云 ECS、VS Code SSH、Docker Compose、kind |
| 演示业务 | `agent-demo/order-service`，登记期望 2 个副本，独立模拟下游 |
| 只读能力 | 资源、事件、日志与登记业务检查采集；Runbook 检索；结构化诊断 |
| 受控写能力 | 登记 Service selector、readiness HTTP 探针 path/port；必须通过程序校验和人工审批 |
| 审计与复查 | 检查点、审批/执行记录、处置后证据、逐次模型调用调试记录、独立只读复查历史 |

## 相比此前版本的改动

| 范围 | 已实现改动 | 价值 |
| --- | --- | --- |
| Day21 → v0.2 基础 | 版本化 service profile、订单 Demo、集群内登记业务检查、十个固定案例 | 从单纯资源排障扩展到应用与业务状态的联合判断 |
| 恢复闭环 | 处置后重新采集资源与业务检查，保存 `post_repair_evidence`、`unverified_scope` | 不以工具写入成功或 Pod Ready 代替业务恢复验收 |
| 恢复边界 | 登记副本数；区分探针配置匹配、漂移和未知；严格识别退出中的旧 Pod | 减少副本不足和配置偏差漏判，避免旧故障 Pod 退出期间误报 |
| 诊断校验反馈 | 动态 `output_contract`；已支持配置假设必须引用相应配置证据；失败进入已有有限重试 | 在诊断阶段暴露依据缺失，避免到规划阶段才退成人工调查 |
| 模型可观测性 | `llm_debug` 记录调用原文、解析结果、错误、反馈、耗时和实际用量；页面复制/下载完整事件 JSON | 区分生成错误、解析错误、校验失败与模型调用成本 |
| 人工处理后复查 | 新增只读复查 API、CLI、独立 PostgreSQL 记录和分页历史 | 验证人工处理后的当前状态，保留原始事件历史，不把恢复归功于 Agent |

详细变更与验收范围见 [CHANGELOG.md](CHANGELOG.md)，阅读入口见 [代码阅读指南](docs/CODE_READING.md)。

## 工作流与系统结构

请求校验 → 采集 → Runbook 检索 → 诊断与校验 → 规划与校验 → 人工审批 → 执行前现场复核 → 固定工具写入 → 资源恢复检查 → 重新采集并验证登记业务。

证据不足和未发现故障的诊断跳过规划；人工调查计划结束自动处置流程。人工处理后通过独立复查接口观察当前状态，不恢复旧审批、不重放写操作，也不覆盖原诊断或原 Recovery verification。

Compose 运行数据库、后端和 Web 页面。后端通过 Kubernetes API 读取集群，并通过 API Service proxy 访问专用业务检查器；检查器在集群内解析登记 Service DNS、核对 ClusterIP，再通过该 Service 发出业务请求。因此后端无需直接解析集群 DNS，业务检查也不使用业务 Service 的 port-forward 代替真实 Service 转发路径。

## 校验职责与执行边界

| 层次 | 校验对象与目的 | 失败行为及主要代码 |
| --- | --- | --- |
| JSON / Schema | 请求和模型输出的字段、类型、枚举及结构 | 拒绝非法请求；模型输出按已有重试预算处理，耗尽后失败。`schemas/`、`agent/schemas.py`、`llm/` |
| 证据引用 | Evidence / Runbook 引用有效性，以及配置假设是否引用必需配置证据 | 给出诊断反馈，拒绝无依据的配置假设。`agent/diagnosis_policy.py`、`nodes.py` |
| 事实一致性 | 资源状态、业务结果、探针配置与模型断言是否相容 | 拒绝矛盾输出；特定证据不足场景由规则保守报告未知。`diagnosis_policy.py` |
| 动作授权 | 登记对象、允许动作、参数、配置依据和计划一致性 | 不生成可执行授权或阻止执行。`remediation_policy.py`、`execution_policy.py` |
| 审批绑定 | 将审批绑定到事件、计划、配置、诊断和证据快照 | 旧审批不能授权变更后的计划。`agent/approval.py` |
| 现场变化与冲突 | 执行前重新读取 profile 和 Deployment 身份/版本；写入时比较旧值和 resourceVersion | 拒绝不匹配或冲突，不静默覆盖。`service_profiles/registry.py`、`tools/remediation_tools.py` |
| 恢复与只读复查 | 新采集资源、登记业务和覆盖范围 | 明确失败/未知和未验证项，不沿用旧成功。`business_recovery.py`、`services/recheck_service.py` |

以上代码路径除显式目录外均相对于 `backend/app/`。审批前检查不能替代执行前现场复核：两次检查之间可能发生发布或人工修改。应用层 PATCH 参数限制也不能替代 Kubernetes RBAC；RBAC 的资源权限不等于字段级授权。

提示词负责说明输出契约；确定性规则负责状态计算与执行门槛；受控报告仅覆盖已实现的部分业务失败说明。不会因模型漏填而自动补写无依据结论。并非所有诊断正文均由程序生成，也尚未完成所有校验错误的统一聚合；部分失败仍可能逐轮暴露。

当前边界：

- Agent 不执行任意 Shell、任意补丁或任意 URL 调查；故障注入脚本由操作者运行，不属于 Agent 工具集。
- profile 与应用版本、镜像声明匹配不等于镜像签名或内容可信证明；`matched` 也不表示服务健康。
- readiness 失败可能来自依赖故障，不直接等同于探针配置错误；业务失败不能通过放宽探针掩盖。
- 尚未完成真实受限 ServiceAccount 的越界验收、登录认证和审批角色授权；当前定位为受控单人演示，认证与角色授权安排在多人试用前补充。
- 现有乐观冲突检查不是跨资源事务，也不证明完整并发执行互斥或 exactly-once；Service 完整身份绑定等仍有改进空间。
- 不宣称支持完整多租户、多集群或自主开放式调查。

## 恢复检查究竟验证了什么

资源恢复检查默认在 60 秒预算内轮询，不满足条件时约每 2 秒再观察，首次成功即结束；API 异常不是持续重试条件，外部调用耗时也可能令实际总耗时超过该预算。资源检查通过后，重新采集一次资源和登记业务证据。

**当前没有连续多轮业务成功判定，也没有延时稳定性复查。** 手动重复复查会产生多条独立记录，不等于后台调度的多轮验证。`passed` 仅说明本次登记范围通过，不覆盖所有副本、集群外入口、未登记接口或长期稳定性。

Deployment 修改 Pod 模板后，会逐步创建新 Pod 并退出旧 Pod。旧 Pod 尚未删除，不代表它仍接收新流量。恢复判定只在严格匹配的 EndpointSlice 记录明确满足 `terminating=true / ready=false / serving=false` 等条件时，从判定视图排除对应旧 Pod；原始证据保留，最终删除仍列入未验证范围。不能简单忽略所有 NotReady 或 Terminating Pod。

## 人工处理后的只读复查

```bash
python -m scripts.recheck_incident --incident-id YOUR_INCIDENT_ID --note '人工处理后复查' --expect passed
python -m scripts.recheck_incident --incident-id YOUR_INCIDENT_ID --history
```

POST `/api/v1/incidents/{incident_id}/rechecks` 触发采集，GET 同一路径读取独立历史。复查不调用模型、不修改 Kubernetes；原事件状态不变。`status` 为 `passed / failed / unknown`，`target_comparison` 单独说明登记摘要、Deployment UID/generation 是否变化，`recovery_attribution=not_established` 不建立修复因果归属。人工备注为未验证说明，不作为诊断事实或已认证身份。

等待审批或执行中的事件拒绝复查。重复 POST 创建不同记录，不具备请求幂等性；请求失败后应先读历史，避免自动重复提交。当前 Web 尚无复查入口，使用 API/CLI；下一阶段补齐页面交互。接口及验收命令见 [只读复查说明](docs/stage2-readonly-rechecks.md)。

## 环境与启动

适用环境：Linux 云服务器、Python 3.12、Docker Compose、kubectl、kind；通过 VS Code SSH 开发。以下命令在仓库根目录执行。已有验收环境不必重新安装或重建集群。

首次准备 Python 环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e . pytest
```

首次使用时从 `.env.example` 复制 `.env`，填写 PostgreSQL、模型和 Embedding 配置。主要设置为 `PGVECTOR_URL`、`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`LLM_MODEL`、`EMBEDDING_MODEL` 和 `KUBERNETES_CONTEXT`。实际默认值以 `backend/app/rag/settings.py` 为准。不要覆盖现有 `.env`。

本项目演示脚本固定使用 `kind-incident-agent`，对应 kind 集群名 `incident-agent`。没有该集群时创建：

```bash
kind create cluster --name incident-agent
```

首次部署或明确需要重建演示应用时执行：

```bash
bash scripts/demo_v02.sh deploy
bash scripts/business_probe_up.sh
bash scripts/compose_up.sh
```

`compose_up.sh` 会检查环境、创建或复用外部 PostgreSQL 卷、构建应用镜像，并在没有 Runbook 向量时建立索引。默认外部卷名为 `postgres_incident-agent-postgres`。已有数据卷会复用。

页面地址为服务器本机 `http://127.0.0.1:8080`，后端就绪检查为 `http://127.0.0.1:8000/readyz`。可使用 VS Code SSH 端口转发在开发机访问页面。

## 服务配置与演示接口

登记文件：`config/service-profiles/agent-demo.order-service.json`。当前配置修订号为 3，期望副本数为 2，关联 `agent-demo/order-service` Deployment 与同名 Service，应用版本为 `order-demo-v0.2.0`，镜像为 `k8s-incident-demo:0.2.0`。

| 路径 | 用途 |
| --- | --- |
| `/livez` | 存活检查，不以模拟下游可用作为存活条件 |
| `/readyz` | 就绪检查，依赖模拟下游可用 |
| `/api/orders/demo-001` | 登记业务接口，预期 HTTP 200，订单 ID、确认状态和依赖状态匹配 |

服务配置记录 `owner` 与 `config_source`。现场修复后仍需同步修改配置仓库，避免下次发布覆盖现场修复；自动同步配置仓库未实现。修改登记配置或重建 Service 导致 ClusterIP 改变后，需要重新运行 `scripts/business_probe_up.sh` 更新检查器登记目标。

仅修改已挂载的后端 Python 通常只需重启 backend；依赖变化需重建后端镜像。前端源码未挂载，修改前端需单独构建。已有环境本次无需重新部署 Demo 或重建前端。

## 检查与固定案例

只读检查当前服务：

```bash
python -m scripts.check_service_profile
python -m scripts.check_business_service --expect passed
python -m scripts.check_diagnosis_policy --llm --expect-resource ready --expect-business passed
```

最后一条调用真实诊断模型；前提是当前 Demo 正常。`check_diagnosis_policy` 不执行处置计划。

第五阶段程序回归：

```bash
python -m pytest backend/tests/fault_cases -q
```

该阶段登记的回归预期为 **16 passed**；本次文档存档未重新执行这组历史测试。十个案例的输入、预期和特殊断言在 `evals/cases/v02-stage5/catalog.json`；回归使用合成 Evidence 和固定模型响应，不能作为模型准确率数据。

真实集群事实批测（会由操作者脚本修改 Demo）：

```bash
bash scripts/run_stage5_live.sh
```

预期七个 `PASS ... (live facts only)`，随后恢复正常并通过恢复后的事实检查。批测不调用诊断/规划模型，也不执行 Agent 审批流程。必要时单独恢复：

```bash
bash scripts/stage5_fault.sh reset
```

| 案例 ID | 输入方式 | 预期资源 / 业务 |
| --- | --- | --- |
| `normal` | 真实集群 / 回放 | ready / passed |
| `selector_mismatch` | 真实集群 / 回放 | not_ready / unknown |
| `readiness_path_error` | 真实集群 / 回放 | not_ready / passed |
| `dependency_unavailable` | 真实集群 / 回放 | not_ready / unknown |
| `api500` | 真实集群 / 回放 | ready / failed |
| `wrong_content` | 真实集群 / 回放 | ready / failed |
| `historical_recovered` | 合成回放 | ready / passed |
| `evidence_missing` | 真实集群 / 回放 | ready / unknown |
| `log_instruction` | 合成回放 | not_ready / unknown |
| `changed_after_approval` | 合成回放 | not_ready / unknown；写入前冲突拦截 |

探针路径错误的真实场景保留旧就绪副本服务流量，因此固定预期为资源未就绪、业务通过；不能强行要求两项状态一致。

按需执行单个案例：

```bash
python -m scripts.run_fault_case --case changed_after_approval --source replay --mode regression
python -m scripts.run_fault_case --case wrong_content --source replay --mode agent
```

`--mode facts` 只检查事实；`regression` 使用固定响应检验程序；`agent` 使用真实诊断及适用时的规划模型，但不执行真实审批和资源修改。`agent` 回放使用冻结 Runbook，不评估实时检索。模型调用可能产生费用。

每次结果写到独立的 `evals/results/stage5/` 子目录，包含 `input.json`、`output-state.json`、`result.json`。记录来源、输入摘要、断言、实际耗时及模型报告的 Token；没有模型用量时不补造数字。`rule_precheck` 表示本地规则生成诊断，不能算作模型成功；该标识也不能证明上游检索没有产生 Embedding 用量。

## 验收范围与下一步

| 层级 | 当前记录 | 不能据此推导 |
| --- | --- | --- |
| 合成程序回归 | 固定案例已通过；后续定向测试分别为 183、117、50 项通过，各批有重叠，不累加为总数 | 真实模型准确率或真实写入权限安全 |
| 真实集群 facts | 维护者已反馈七个场景通过 | 模型诊断、规划或自动修复通过 |
| 真实模型 / 写操作 | selector、readiness 的真实修复及 Recovery verification 已反馈通过 | 全案例模型稳定或统计意义上的低误报率 |
| 人工复查与持久化 | 维护者已反馈原人工调查事件复查通过、重启后记录仍存在 | 页面交互完成、并发与故障注入全部通过 |
| 本次文档存档 | 文档、注释和局部格式整理；Python AST 等价及补丁应用检查 | 新增业务能力或重跑全部历史验收 |

历史实测结果是维护者反馈；本存档不包含新的真实集群/真实模型测量，也不公开运行环境中的原始诊断数据。不要将不同测试层级混为同一“通过率”。

后续按单阶段验收推进：

1. **未实现：页面复查闭环。** 展示负责人、配置来源、人工说明、复查历史、目标变化和未验证范围，区分旧事件与新观察。
2. **部分完成：校验反馈与边界收敛。** 聚合可同时发现的错误、优化上下文与预算；完善目标身份和并发冲突边界，保留执行前现场复核。
3. **待验收：真实受限 Kubernetes 身份。** 使用实际受限身份验证越界读取/写入拦截；认证、审批角色授权在多人试用前补齐。
4. **未实现：公平的规则/Agent 对比基线与有界只读调查。** 先固定同批输入及计分规则，再增加工具白名单、调用/Token/时间预算和停止条件，让模型按缺失证据选择下一步只读采集；写入仍走既有校验和审批。
5. **未完成：分层实测对比。** 分别报告合成回放、真实 facts、真实模型和真实写操作，记录判断准确性、错误修复建议、业务恢复误报、耗时和实际 Token。当前不填造指标。

存档和推送步骤见 [Git 提交与阶段存档](docs/GIT_PUBLISH.md)。
