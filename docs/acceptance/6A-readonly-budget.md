# 6A：只读工具协议与预算（待 ECS 验收）

queued worker 的诊断、交互与延时复查新增持久预算；模型自主选择工具留到 6B。工具仅接受服务端资源引用，读取前后核对 UID、归属、Deployment generation 和 profile；不接受 namespace、URL、label，不读 Secret、不 exec。Pod 日志每次最多 200 行/12KB，事件 50 条，输出最多 12000 字符；截断标 partial，失败标 unknown，常见凭据脱敏，工具输出始终作为不可信证据保存。

默认每 run：3 次调查决策（追问也计入）、6 次追加工具、90 秒追加读取、300 秒活动；单次模型输入估算 12k、输出上限 2k、累计 40k token。SDK 自动重试关闭；调用前预留，成功按 usage 结算，未知用量保留预留值。人工等待不计时，进程丢失保留在途预留；写前预留 30 秒执行和 120 秒验证。旧同步模式不接入本次 run 预算。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage6a.sh
# PASS 后部署：
python -m scripts.init_persistence
docker restart k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
bash scripts/deploy_stage4c_frontend.sh
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

预期 `PASS: 6A ECS acceptance`；迁移输出包含 `13`（已迁移则 up to date），就绪返回 ready。无新增依赖或 `.env` 配置，继续 queued + worker。浏览器 Ctrl+F5，创建测试事件或继续调查，打开本轮“调试反馈 → 本轮预算”：出现已用时间/Token/决策记录；旧轮允许显示尚无记录。读取接口为 `GET /api/v1/incidents/{incident_id}/runs/{run_id}/budget`，超限保留证据并给出未知范围和人工交接提示。

本地仅静态检查。脚本在隔离 PostgreSQL 验证预算持久化、租约接管、并发预留、权限边界、脱敏截断、写前拒绝及已发写请求核对；模型和 Kubernetes 响应受控，不会发真实修复。失败提供 `evals/results/6a/<测试库>/backend.txt`、`junit.xml` 或 `frontend.txt`。真实模型 usage/延迟、真实追加工具与浏览器部署后效果待 ECS 验证；当前工具数为 0 可以是正常结果，6A 尚无自主追加工具循环。时间限制依赖同步 SDK 超时和阶段检查，不能强制取消已发网络请求；Token 估算和规则脱敏不承诺精确计价或识别所有业务秘密。
