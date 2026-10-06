# 2C 交付

2B 已反馈 PASS。本块增加默认关闭的故障屏障、脱敏结构化时间线、禁止绕过写入核对的重试保护，以及 R01–R06 共 15 个独立进程恢复场景。

涉及：`runtime/{failpoints,telemetry,worker,operations,checkpointer}.py`、`persistence/{runs,leases,operations}.py`、API 启动保护；`tests/runtime`、验收 runner 和 `scripts/accept_stage2c.sh`。无前端修改。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage2c.sh
```

预期：`2C R01-R06: all 15 process recovery scenarios passed.`，最后 `PASS: 2C ECS acceptance`。证据在 `evals/results/2c/`：JUnit、场景计数、各子进程时间线、run/operation、客户端 PATCH 记录与实际资源前后快照。测试库保留。

沿用 2B 的 Python 3.12、PostgreSQL、kind 和凭据；需要在 `agent-demo` 创建/删除临时 Service。测试创建并清理随机名 `stage2c-*`（回归还会创建 `stage2b-*`），不停止现有数据库。测试进程自己配置故障点，不要写入业务 `.env`。

无新增依赖、必填配置或数据库迁移（仍为 6），不需前端重建。先验收；通过后，挂载代码的后端用 `docker restart k8s-incident-agent-backend-1` 加载修改；如另有运行中的应用 worker，也需重启该 worker。不要重启名为 `incident-agent-worker` 的 kind 节点。排队模式配置仍为 `INCIDENT_AGENT_EXECUTION_MODE=queued`。

本地只做 Python AST、内部导入符号、Bash 语法和 Git diff 静态检查，不运行 pytest/npm。动态结果待 ECS 验收。

范围限制：调查/模型调用、发布画像和业务恢复结论使用测试替身，DB、审批 HTTP、租约、Service PATCH 是真实调用；R03 覆盖 waiting_approval；R06 注入存储调用失败，不停 PostgreSQL。客户端 PATCH 记录不是 Kubernetes 服务端审计。真实模型费用、业务闭环、生产 RBAC、数据库丢失灾备及历史遗留测试不在本块验证范围。

复验修正：ECS 首轮 244 passed / 1 failed，新增日志污染子进程 stdout，导致 JSON 解析报 Extra data。现将日志写入 stderr，并断言 stdout 不被污染；原跨进程持久化测试保留。时间线继续收集两路输出。无配置、依赖或迁移变化；拉取后重跑同一命令，动态结果待复验。

维护者已反馈复验成功，阶段 2 收尾，进入 3A。
