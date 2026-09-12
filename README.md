# Kubernetes Incident Agent

面向 Kubernetes 应用发布与运行异常的诊断及受控处置工具。基于 Python、FastAPI、LangGraph、PostgreSQL/pgvector 和 React/TypeScript，使用 Docker Compose 运行后端与页面，使用 kind 运行演示应用及集群内业务检查器。

当前为 **v0.2 开发阶段快照：已推进至阶段 5**。本轮固定案例回归和真实集群事实批测已由维护者在阿里云 ECS 上报告全部通过。本文没有将其标记为完整 v0.2 发布验收；阶段 6–8 尚待实施。Python 包和 API 默认版本号仍为 `0.1.0`，不要用它们判断阶段进度。

## 相比 Day21 的更新

| 阶段 | 已实现内容 | 实现位置 |
| --- | --- | --- |
| 1：版本化服务配置 | 资源关联、负责人、配置来源、版本与镜像匹配、预期 selector/探针、只读业务检查约定；执行前复核配置摘要和 Deployment 身份 | `config/service-profiles/`、`backend/app/service_profiles/` |
| 2：HTTP Demo | 订单服务及独立模拟下游；存活、就绪、业务接口分离；依赖中断、接口 500、响应内容错误场景 | `infra/demo-app/`、`scripts/demo_v02.sh` |
| 3：业务 Evidence | 集群内检查器通过登记 Service 访问接口；验证状态码和 JSON 字段；检查器异常、连接失败保留未知 | `backend/app/business_checks/`、`infra/business-probe/` |
| 4：诊断与修复门槛 | 资源与业务状态分开；症状、根因假设、缺失证据、下一步调查；引用与语义校验；本地证据不足预判；受控报告与模型原始输出审计 | `backend/app/agent/diagnosis_policy.py`、`diagnosis_report.py`、`nodes.py`、`remediation_policy.py` |
| 5：固定案例 | 十个固定案例、七个真实集群事实场景、合成安全回放、独立结果目录与输入摘要 | `evals/cases/v02-stage5/`、`scripts/fault_cases/`、`scripts/run_fault_case.py` |

原有人工审批、审批绑定、并发保护、执行结果复用、检查点和事件持久化机制继续保留。详细变更见 [CHANGELOG.md](CHANGELOG.md)，代码阅读顺序见 [docs/CODE_READING.md](docs/CODE_READING.md)。

## 能力边界

- Agent 写操作仅支持修正 `agent-demo` 内登记对象的 Service selector 和 readiness probe；需要可信配置依据、计划校验和人工审批。
- 应用版本或镜像声明不匹配，或者审批后配置/资源发生变化，不能继续按旧配置写入。镜像字符串匹配不代表镜像签名或内容可信证明。
- 不把 Pod Ready 当作业务恢复；不把 readiness 失败直接当作探针配置错误；业务响应失败时不能靠放宽探针掩盖异常。
- 诊断中的 `resource_status` 与 `business_status` 是本次采集的诊断快照。现有 `verification_result` 仍是处置后的 Kubernetes 资源验证，尚不包含处置后的业务验证。
- `business_status=passed` 仅覆盖登记接口的本次请求，不覆盖集群外入口、未登记接口、每个副本或长期稳定性。
- 依赖故障注入的真实原因已知，但 Agent 当前没有独立下游检查证据；满足本地预判条件时报告 `unknown / insufficient_evidence`，不假装确认依赖根因。
- 尚未完成真实受限 Kubernetes 身份验收、登录认证和审批角色授权。当前适合受控的单人演示环境。
- 操作者脚本可以注入故障；它们不属于 Agent 工具集。没有任意 Shell、自动代码修复、多集群、多 Agent 或 Kafka 架构。

## 运行结构

Compose 运行 PostgreSQL/pgvector、FastAPI 后端与 React 页面。后端通过 Kubernetes API 采集资源，并访问集群内专用检查器。检查器解析登记 Service DNS，核对 ClusterIP 后通过该 Service 地址发出 HTTP 请求。

后端不需要解析集群 DNS。访问检查器所用的 API Service proxy 不等于代理业务 Service；实际业务请求由集群内检查器发出，不使用业务 Service 的 port-forward 证明转发正常。

主要流程：请求校验 → 证据采集 → Runbook 检索 → 结构化诊断与本地校验 → 处置规划 → 人工审批 → 执行前复核 → 固定工具写入 → 资源验证。

证据不足和未检测到故障的诊断跳过处置规划。`diagnosis` 是正式报告，`diagnosis_model_output` 保留模型结构化原始输出供审计。

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

登记文件：`config/service-profiles/agent-demo.order-service.json`。当前关联 `agent-demo/order-service` Deployment 与同名 Service，应用版本为 `order-demo-v0.2.0`，镜像为 `k8s-incident-demo:0.2.0`。

| 路径 | 用途 |
| --- | --- |
| `/livez` | 存活检查，不以模拟下游可用作为存活条件 |
| `/readyz` | 就绪检查，依赖模拟下游可用 |
| `/api/orders/demo-001` | 登记业务接口，预期 HTTP 200，订单 ID、确认状态和依赖状态匹配 |

服务配置记录 `owner` 与 `config_source`。现场修复后仍需同步修改配置仓库，避免下次发布覆盖现场修复；自动同步配置仓库未实现。修改登记配置或重建 Service 导致 ClusterIP 改变后，需要重新运行 `scripts/business_probe_up.sh` 更新检查器登记目标。

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

本轮预期为 **16 passed**。十个案例的输入、预期和特殊断言在 `evals/cases/v02-stage5/catalog.json`；回归使用合成 Evidence 和固定模型响应，不能作为模型准确率数据。

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

## 验收记录与后续

本轮，维护者在阿里云 ECS 上反馈第五阶段全部通过，范围为固定程序回归、七个真实集群事实场景及批测后的恢复事实检查。原始运行记录位于执行环境的 `evals/results/stage5/`，未随源码公开提交。本次没有重新执行全部历史测试，也没有将合成审批回放当作真实受限身份验收。

下一阶段是 **阶段 6：处置后资源与业务验证分开持久化，支持人工处理后重新检查，补齐页面负责人、处理结果和未验证范围**。随后是阶段 7 真实受限身份与越界验收，以及阶段 8 同批案例的规则/Agent 实测对比。当前不声明模型判断准确率、错误修复率或业务恢复误报率。

提交与推送流程见 [docs/GIT_PUBLISH.md](docs/GIT_PUBLISH.md)。
