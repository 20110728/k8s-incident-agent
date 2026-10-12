# 0A ECS 回归反馈：审批与执行测试输入修复

## 反馈与定位

2026-10-04，维护者在 ECS 对 0A 审批/执行/持久化命令反馈：
**14 failed、76 passed、2 warnings**。来源为维护者粘贴的 pytest 输出，
不属于本地实测，也不能据此宣称其他验收组通过。

- `test_graph_human_approval.py` 3 项失败，`test_graph_execution.py` 7 项失败。
  图在诊断阶段进入 diagnosis_failed，没有进入期待的审批 interrupt。
- `test_execution_policy.py` 4 项失败，错误为 selector 修复缺少允许执行的依据；
  与旧 STAGE6_1_ACCEPTANCE.md 记录的四项失败名称一致。
- 两条 Starlette HTTP 422 常量弃用警告不是上述断言失败的原因，本次不处理。

静态对照 `67f310d` 与 `4178196`：相关旧测试和 backend/app 均未变化。
测试输入没有跟进当前契约：诊断缺 assessment=v2，Deployment 缺观察到的
readiness 配置，部分 Service 缺 namespace/name，计划和诊断缺 Deployment 引用。
当前诊断/写入规则要求这些字段，不能为了测试通过删除校验。

## 改动

- 新增 `backend/tests/agent/selector_fixtures.py`：明确的 v2 selector assessment，
  resource_status=not_ready、business_status=unknown；预期值来自合成场景定义，
  不调用被测 policy 生成预期值。
- 更新 `test_graph_human_approval.py`：补 Pod namespace、归属链、Deployment 副本和
  readiness 配置、v2 assessment 及 Service/Deployment 配置引用。
- 更新 `test_execution_policy.py`：补齐同类测试输入，保留原拒绝/防重放断言；
  新增一项正向契约检查，以及缺 assessment/探针/Deployment 引用的三项拒绝检查。
- 更新 `test_graph_execution.py`：interrupt 断言失败时附带 errors，方便下次直接定位。
  该文件继续使用 human_approval 中修正的共享输入。
- 应用代码、旧案例 expected、冻结兼容 JSON、共享 legacy profile 均不修改。

本地按约定只做静态检查：4 个变更 Python 文件 AST 可解析；原有测试函数均保留，
原断言条件未更改（仅为两个断言增加 errors 消息）；`git diff --check` 通过。
**未在本地运行 pytest/工作流，修复效果待 ECS 确认。**

## ECS 复验

在原仓库根目录、Python 3.12 虚拟环境执行。先确认工作区无本地修改，再拉取：

```bash
git status --short
git pull --ff-only origin feature/baseline-contracts
git log -1 --oneline
set -o pipefail
```

复用原 AUDIT_DIR；若新开终端，先建立新目录：

```bash
umask 077
export AUDIT_DIR=$(mktemp -d "$HOME/k8s-0a-fix-XXXXXXXX")
python -m pytest \
  backend/tests/agent/test_graph_human_approval.py \
  backend/tests/agent/test_graph_execution.py \
  backend/tests/agent/test_execution_policy.py \
  backend/tests/api/test_incident_approval_service.py \
  backend/tests/api/test_persistent_incident_service.py \
  backend/tests/api/test_rechecks.py \
  backend/tests/persistence \
  -q 2>&1 | tee "$AUDIT_DIR/persistence-approval-fixed.txt"
```

预期 **94 passed、0 failed**（原 90 项 + 新增 4 项）；这是验收目标，尚未实测。
依赖版本导致的非失败 warning 可另记，不能把 failures 当作 warnings。

一个已有 profile 测试也使用这组共享图输入，定向复验：

```bash
python -m pytest \
  backend/tests/service_profiles/test_stage1.py::test_profile_survives_graph_checkpoint \
  -q 2>&1 | tee "$AUDIT_DIR/profile-checkpoint-fixed.txt"
```

预期 **1 passed**。无其他代码变化时，无需为本修复重跑前端或真实故障注入。
已完成的其他 0A 验收结果可以继续沿用；未完成的仍按原 0A 文档执行。

配置/依赖/迁移/重启要求：**全部无**。测试不调用真实模型、不写 Kubernetes，
持久化 mock 测试与 InMemorySaver 不证明 PostgreSQL 重启恢复。
收到上述复验结果后再更新 0A 结论，不推进新验收块。
