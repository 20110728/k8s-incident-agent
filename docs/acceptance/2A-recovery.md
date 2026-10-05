# 2A 交付

1B 已由维护者反馈验证通过；本块只改恢复代码及测试，不改前端。

- `runtime/recovery.py`、`worker.py`：只读 pending task 用原 thread + invoke(None) 继续；END 只补状态，interrupt 保持等待；写操作现场转 reconciling，身份/输入/版本不符或坏 checkpoint 停止。
- `persistence/leases.py`、`runtime/checkpointer.py`、migration 5：写 checkpoint 前持久化标记，防止丢失后从头执行；达到 3 次预算仍可补终态，但禁止继续执行节点。旧的已尝试任务保守标记，不自动重跑旧失败记录。
- `tests/runtime/test_recovery*`：真实进程在采集后、模型节点内、诊断后被 kill；检查已完成节点不重复、待执行只读节点续跑、审批不越过，以及临时错误/永久失败/缺失检查点。
- 复用已有验收入口，避免重复脚本。本地仅 Python AST、Git diff、Bash 语法静态检查；ECS 运行结果待反馈。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage2a.sh
```

预期无失败/错误，真实数据库用例不跳过，最后 `PASS: 2A ECS acceptance`；日志在 `evals/results/2a/`。
自动新建并保留独立测试库，迁移到 5，不动演示库；本次不跑前端测试，不需要 Node/npm。
无新依赖/配置。先验收，不必重启。正式加载时需先停 worker，再重启 backend 自动迁移至 5，最后启动 worker；仍保持 sync 演示，queued 审批执行待 2B。
未验证：ECS 动态结果、真实模型/集群崩溃恢复。合成测试通过不等于外部写可重放；已保存的业务失败 END 不自动重试。
