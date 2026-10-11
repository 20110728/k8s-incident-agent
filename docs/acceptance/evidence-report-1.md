# 块 1：依赖访问失败可观测性

范围：仅 `infra/demo-app/demo_app/server.py` 和 Demo 测试。计划见 [分块计划](../有用证据与稳定报告-分块计划.md)。当前已实现，ECS 待验收。

## 行为

已有依赖请求失败时，每次输出一条 JSON `dependency_request_failed`：UTC 时间、服务角色、readiness/business 用途、目标主机、固定操作路径、失败原因、实际 HTTP 状态（未收到时为 null），以及可用的异常类型、底层异常类型和 errno。

区分 `timeout`、`request_error`、`http_status_mismatch`、`response_too_large`、`invalid_json`、`response_assertion_failed`。操作路径不是完整 URL；不输出 URL 凭据、查询参数、响应正文或异常消息。不能由这些日志推断下游副本数或注入动作。

不新增请求、重试、依赖、配置或迁移；不改原有 HTTP 响应、超时、响应大小上限、重定向限制和 Agent 权限。每次失败均记录，避免持续故障时只有启动阶段留有原因；日志量会增加，采样仍受现有 1000 行/256 KiB 限制，时间覆盖需据实际采样判断。

## ECS 命令

在项目根目录、已激活 Python 3.12 环境执行。先通过 Git 同步本块代码，核对 SHA 和工作区；不要覆盖 ECS 未提交修改。

```bash
git branch --show-current
git rev-parse HEAD
git status --short
python -m pytest backend/tests/demo_app/test_demo_v02.py -q
```

预期：测试通过；覆盖连接失败、真实超时、HTTP/重定向、响应过大、JSON/内容校验、正常路径、用途标记及敏感内容不进入新日志。受控测试不证明真实模型效果。

在 Demo 正常场景下部署；如仍在故障状态，先恢复再构建。以下均是操作者命令，不是 Agent 工具：

```bash
bash scripts/stage5_fault.sh reset
bash scripts/demo_v02.sh deploy
kubectl --context kind-incident-agent -n agent-demo rollout restart deployment/order-service deployment/order-dependency
kubectl --context kind-incident-agent -n agent-demo rollout status deployment/order-service --timeout=120s
kubectl --context kind-incident-agent -n agent-demo rollout status deployment/order-dependency --timeout=120s
bash scripts/stage5_fault.sh dependency_unavailable

mkdir -p evals/results/evidence-report-1
git rev-parse HEAD > evals/results/evidence-report-1/commit.txt
kubectl --context kind-incident-agent -n agent-demo logs \
  -l app=order-service -c order-service \
  --tail=1000 --timestamps=true --prefix=true \
  > evals/results/evidence-report-1/pod-logs.txt
```

当前镜像标签复用 `0.2.0`，因此加载镜像后必须替换旧 Pod，不能把 `apply` 成功当作新代码生效。无须因本块重启 Agent 后端、worker 或前端。

在页面创建新事件：目标 `agent-demo/order-service`，描述“订单接口不可用，请调查原因”。不附注入命令、场景名或副本数。等真实调查结束，使用该事件及该 run 的 ID：

```bash
python -m scripts.export_investigation_debug --incident-id <事件ID> --run-id <任务ID> --brief
python -m scripts.export_investigation_debug --incident-id <事件ID> --run-id <任务ID> --last-llm
python -m scripts.export_investigation_debug --incident-id <事件ID> --run-id <任务ID>
```

导出路径由命令打印；第三条保留完整排障记录。记录这三个文件、事件/run ID 和页面报告。导出只读取保存数据，不重新调用模型或集群。

## 必须分别记录的结果

1. **证据产生与采集**：两份 Pod 当前日志的采样中有带目标和真实原因的失败记录；保存证据也包含这些记录。故障持续后仍有新失败记录，不仅是启动消息。
2. **证据入模**：`last-llm` 的实际 user message 中有同一证据 ID 对应的失败日志；不能只看 kubectl 输出。如果最后一次调用没有包含关键证据，本项不通过。
3. **真实报告有效性**：报告引用当前日志，解释依赖访问失败与就绪失败的关联；具体下游原因保留待验证。不得宣称已查明缩容为零、当前 OOM、探针配置错误或已恢复；不生成不适用写计划。unknown 可以通过，但必须说清已知事实、缺失的下游证据和由负责人核查实际依赖状态等具体步骤。
4. **判定**：只通过 1/2 时记录“证据层通过，端到端未通过”；若仍被诊断契约拒绝或只给笼统兜底，保留错误和报告，不能宣称模型有效，也不在本块混入报告修复。

原先只有探针状态码的案例不能据此被补写依赖根因；无关键证据的报告回归属于块 3。当前证据只支持应用观察到的依赖访问失败，不支持独立下游健康结论。

完成取证后恢复，并确认命令成功：

```bash
bash scripts/stage5_fault.sh reset
```

## 本地检查与未验证项

本地只做 Python AST、差异和契约静态检查；未运行 pytest、数据库、真实模型、集群或浏览器。ECS 测试、镜像更新、采样时间覆盖、真实入模及报告质量均待验收。块 2/3 尚未实施。
