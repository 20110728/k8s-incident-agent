# 5B：持久化延时复查（待 ECS 验收）

新连续观察通过后，在同一事务登记一次 30 秒后的只读复查，按观察/策略版本去重；普通单次检查不登记，旧 5A 结果不批量补排。等待不占事件名额，到点沿用 worker 任务租约。事件忙则延期，超过到期时间 5 分钟标过期；新轮次/控制修订或现场身份、profile 变化则失效。初次结论与延时结果独立，复发不触发修复或回滚。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage5b.sh
# PASS 后：
python -m scripts.init_persistence
docker restart k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
bash scripts/deploy_stage4c_frontend.sh
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

预期 `PASS: 5B ECS acceptance`。迁移 12；无新增配置/依赖版本，必须保留应用 worker。测试使用隔离 PostgreSQL、新进程接管和受控采样，覆盖去重、忙碌、期限、失效、复发及结果提交后重启。失败提供 `evals/results/5b/<测试库>/backend.txt` 或 `frontend.txt`。

浏览器 Ctrl+F5，对已结束测试事件点“观察稳定性”；通过后在“执行与复查”看到等待延时复查，30 秒后等待 worker 实际执行，记录实际采样时间和独立结果。未能通过初次窗口时不安排延时检查。停止 worker 超过期限再启动应过期，不能伪造通过；不要在生产修复过程中做故障注入。

本地仅静态检查。真实集群复发、真实 Kubernetes 网络中断与新版浏览器完整交互仍未验证。中断时可能再次发出只读采样，不承诺物理请求恰好一次；已保存的终态结果重放不重采。阶段 6 未开始。
