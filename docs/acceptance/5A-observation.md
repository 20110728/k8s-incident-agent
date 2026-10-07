# 5A：有限连续观察（待 ECS 验收）

自动修复验证改为连续窗口；单次重新检查保持原行为，新增明确按钮“观察稳定性”。默认资源 60 秒、业务 30 秒、总计上限 120 秒，至少连续 3 次通过，采样完成后间隔至少 5 秒。失败/未知清零计数；UID、profile 或部署代次变化使窗口失效。无模型调用、无新增修复或回滚。

迁移 11 保存策略、截止时间、目标身份及每次采样子记录。重启重新计连续次数，不重置预算；完成窗口重放不重采。请求禁用 SDK 重试，超时限制使用剩余预算；最多一个在途调用存在退出宽限，远端探针不保证随进程退出取消，不承诺绝对硬实时。仍未覆盖所有副本及采样后的持续可用性。5B 延时复查未实现。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage5a.sh
# PASS 后部署：
python -m scripts.init_persistence
docker restart k8s-incident-agent-backend-1 k8s-incident-agent-worker-1
bash scripts/deploy_stage4c_frontend.sh
curl --fail --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8000/readyz
```

预期 `PASS: 5A ECS acceptance`。脚本使用隔离 PostgreSQL，确定性采样覆盖失败/未知夹杂、目标变化、资源复发、超时、恢复及旧租约隔离，运行前端测试与构建；不使用真实模型、不修改集群。本地只静态检查，无依赖版本/额外环境配置变更。失败提供输出目录的 `backend.txt` 或 `frontend.txt`。

部署后 Ctrl+F5，在已结束且无待核对操作的测试事件点“观察稳定性”，在“执行与复查”查看采样明细；健康目标应连续通过，缺少登记/证据时应保持未确认。原结论保留，普通重新检查无连续窗口。真实集群耗时、真实进程中断恢复及自动修复后窗口仍需 ECS 实测。
