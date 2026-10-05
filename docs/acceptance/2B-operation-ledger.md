# 2B 交付

2A 已由维护者反馈 PASS。本块：queued 审批先落库；worker 使用稳定 operation_id、UID/版本/旧值条件式 PATCH；保存真实响应；结果不明只读核对并停在 manual_required。旧 sync 流程保留。

涉及：`persistence/operations.py`、migration 6；`runtime/operations.py`、worker/recovery；审批服务与 operations 查询接口；Service UID 采集、业务验证、前端审批提示及针对性测试。

```bash
cd ~/projects/k8s-incident-agent
git pull --ff-only origin feature/baseline-contracts
source .venv/bin/activate
bash scripts/accept_stage2b.sh
```

预期：无失败；本块真实数据库和 kind 用例不跳过；最后 `PASS: 2B ECS acceptance`。日志在 `evals/results/2b/`。独立测试库自动创建并保留，迁移至 6；kind 用当前凭据在 `agent-demo` 创建并清理随机名 `stage2b-*` Service，不修改现有 Service/Deployment。需要对应创建/删除权限。仅排查模拟测试可用 `STAGE2B_KIND=0 bash scripts/accept_stage2b.sh`，不会输出完整验收 PASS。前端会执行 npm ci/test/build。

无新增依赖、无新增必填配置。先验收，不必重启；通过后加载：先停运行中的 worker，重启 backend 自动迁移至 6，再启 worker；前端需重建。Compose 沿用现有 LOCAL_UID/LOCAL_GID/KUBECONFIG_PATH。启用排队执行仍使用 `.env` 的 `INCIDENT_AGENT_API_EXECUTION_MODE=queued` 并重建 backend 容器；已启用无需改配置。

查询操作证据：`GET /api/v1/incidents/{incident_id}/operations`。`manual_required` 保留观测与未知归属，run 保持 reconciling，不自动重试/回滚；此块不提供人工关闭接口。

旧 queued 审批如果缺少 Service UID，会拒绝执行，不能补填 UID 后复用原审批；需重新创建事件取证和审批。

本地 Python AST（176 个文件）、新增模块内部导入符号、Bash 语法、Git diff 检查通过；未执行 pytest/npm 或集群操作。待验证：ECS 全部动态结果；真实模型/业务闭环、受限身份和独立进程精确 kill 窗口。kind 用例是实际 Service PATCH + PostgreSQL，登记画像使用测试快照，不等于验证生产画像/RBAC；更完整恢复故障工具留在 2C。

复验修正：首次 ECS 日志为 415 passed / 29 failed，失败来自 5 个旧诊断/规划/profile 测试文件，其样例与当前诊断契约不一致。验收入口误纳入整个旧目录，现明确限定为全部 persistence/API/runtime（含 2B 数据库和 kind）及审批、执行、业务验证相关回归；不改生产校验、不删除或标记跳过旧测试。29 项旧测试适配仍未解决，不宣称全仓测试通过。仅修改验收入口，无新增迁移、依赖、配置或重启要求；拉取后重跑同一命令，前端在后端测试通过后继续验收。
