# K8s Incident Agent

面向将业务部署到 Kubernetes 后需要排障的开发者，提供可追溯的诊断、人工审批后的受控修复，以及资源和登记业务接口的恢复检查。

项目采用 LangGraph 持久化工作流与有界多轮调查：模型选择取证、追问、诊断和修复候选；程序负责计算事实、校验引用与权限、生成固定修复参数并控制审批执行。

**当前状态：2026-10-12，核心功能进入收尾验收，尚未声明新正式版本发布。** 多轮 selector 已获维护者反馈完成诊断、审批、执行及连续 3/3 资源与业务观察；dependency 实际调用记录已验证关键日志入模及保守诊断。当前多轮 readiness 最后一次受控修复正在验收，独立恢复导出及最新回归尚待 ECS 反馈。历史单轮 readiness 通过不代替本轮验收。Python 包/API 版本仍为 `0.1.0`。

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

请求持久化并返回 ID → worker 领取任务 → 基线采集与 Runbook 检索 → 按需补采/追问/诊断 → 引用及语义校验 → 程序生成受控计划 → 人工审批 → 现场复核与操作账本 → 固定工具写入 → 资源及登记业务连续观察。

新调查最多 5 轮决策（追问占轮次）、6 次模型调用（含失败和纠正）；不设置累计 Token、总时间或工具总次数额度。保留单次网络超时和独立恢复观察窗口。旧单轮兼容路径仍保留。

证据不足和未发现故障的诊断跳过规划；人工调查计划结束自动处置流程。人工处理后通过独立复查接口观察当前状态，不恢复旧审批、不重放写操作，也不覆盖原诊断或原 Recovery verification。

Compose 运行数据库、后端、独立 worker（worker profile）和 Web 页面。后端通过 Kubernetes API 读取集群，并通过 API Service proxy 访问专用业务检查器；检查器在集群内解析登记 Service DNS、核对 ClusterIP，再通过该 Service 发出业务请求。因此后端无需直接解析集群 DNS，业务检查也不使用业务 Service 的 port-forward 代替真实 Service 转发路径。

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
- 目标身份绑定与受限身份验收已有阶段 3 实现及维护者历史通过记录；登录认证和审批角色授权仍未完成，当前定位为受控单人演示。
- 租约、资源 UID/版本、审批绑定及操作账本用于控制执行与恢复；外部写结果不确定时先核对，不宣称跨数据库与 Kubernetes 的 exactly-once。
- 不宣称支持完整多租户、多集群或自主开放式调查。

## 恢复检查究竟验证了什么

恢复使用 `recovery-window-v1`，默认要求连续 3 次资源与登记业务采样通过，资源/业务/总窗口分别保留 60/30/120 秒限制。重启重新计算连续次数，不延长原截止时间。已有单次复查、稳定性观察与持久化延时复查能力。

通过仅覆盖登记资源与业务采样，不代表所有副本、集群外入口或持续可用性。selector 资源检查直接核对批准 selector 与就绪端点，副本统计字段为空不表示失败。

Deployment 修改 Pod 模板后，会逐步创建新 Pod 并退出旧 Pod。旧 Pod 尚未删除，不代表它仍接收新流量。恢复判定只在严格匹配的 EndpointSlice 记录明确满足 `terminating=true / ready=false / serving=false` 等条件时，从判定视图排除对应旧 Pod；原始证据保留，最终删除仍列入未验证范围。不能简单忽略所有 NotReady 或 Terminating Pod。

## 人工处理后的只读复查

```bash
python -m scripts.recheck_incident --incident-id YOUR_INCIDENT_ID --note '人工处理后复查' --expect passed
python -m scripts.recheck_incident --incident-id YOUR_INCIDENT_ID --history
```

POST `/api/v1/incidents/{incident_id}/rechecks` 触发采集，GET 同一路径读取独立历史。复查不调用模型、不修改 Kubernetes；原事件状态不变。`status` 为 `passed / failed / unknown`，`target_comparison` 单独说明登记摘要、Deployment UID/generation 是否变化，`recovery_attribution=not_established` 不建立修复因果归属。人工备注为未验证说明，不作为诊断事实或已认证身份。

页面已支持“重新检查”“观察稳定性”及历史查看。旧独立复查 API/CLI 继续兼容，其请求语义不能与页面控制接口混同。历史接口见 [只读复查说明](docs/stage2-readonly-rechecks.md)，当前页面与观察机制见 [4C](docs/acceptance/4C-workbench.md)、[5A](docs/acceptance/5A-observation.md)、[5B](docs/acceptance/5B-delayed-recheck.md)。

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

后端/调查逻辑或模型环境变量变更需同时更新 backend 与 worker，入口为 `bash scripts/deploy_stage6b2b_backend.sh`，先等待活动任务结束。前端单独构建，Demo 仅在应用变更时重部署。文档和本地导出 CLI 更新无需重启服务。

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

## 收尾验收与排障导出

当前核心回归在 ECS 运行 `bash scripts/accept_stage6bc2.sh`，使用受控模型/集群和真实隔离 PostgreSQL，不等同于真实模型效果。早期通过情况来自维护者历史反馈，不代表本提交重跑过全部测试。

- 已有真实反馈：多轮 selector 审批修复与连续观察；dependency 日志取证及保守诊断。
- 待最后反馈：当前多轮 readiness 修复、恢复审计导出及最新回归结果。
- 后续范围：完整 unknown 报告增强、检索优化、复杂场景公平对照和效果统计，不继续扩展为本次必做功能。

```bash
python -m scripts.export_investigation_debug --incident-id YOUR_INCIDENT_ID --brief
python -m scripts.export_investigation_debug --incident-id YOUR_INCIDENT_ID --last-llm
python -m scripts.export_remediation_debug --incident-id YOUR_INCIDENT_ID
```

可用 `--run-id` 指定历史诊断轮次。恢复导出单独写入 `evals/results/remediation-debug/`，包含保存的计划、审批、实际 patch、执行与复查记录，不重新调用模型或集群。详见 [恢复导出验收](docs/acceptance/remediation-export.md)。

待 readiness 和最后回归确认后固定发布提交、归档演示与未覆盖范围；不填写未经测量的准确率或成本降幅。
