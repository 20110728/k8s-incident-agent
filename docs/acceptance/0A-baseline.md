# 阶段 0 / 块 0A：基线审计与最小回归

- 日期：2026-10-04（Asia/Shanghai）。
- 基线 SHA：`67f310d184eb91c6c07a21752c832543eb3301ae`，与开发计划一致。
- 本块范围：基线盘点、旧 phase 映射约定、兼容样本、最小回归及 ECS 交接。
- 结论：**部分通过，待 ECS 验收；不进入 0B/1A**。
- 交付分支：`feature/baseline-contracts`；拉取后以 `git rev-parse HEAD` 记录实际 SHA。
- 应用 migration 声明：1=create_incidents、2=create_rechecks；实际数据库版本待查。
- 真实模型调用 / Kubernetes 写入：本次均无。
- 开发约定：本地只做静态检查；运行测试、集成检查和实际环境验收统一在 ECS 执行。
  下文已执行的本地测试保留为历史记录，不要求本地补装运行环境或重复执行。

## 基线与审计范围

开始时 tracked 文件无改动，仅开发计划 Markdown 未跟踪；保留该文件原文。
全仓 tracked 文件清单及内容 hash 由 `scripts/audit_stage0a.py` 采集，Python 文件
统一做 AST 解析；人工重点审查 API → service → graph → 审批/执行/恢复 →
persistence 及前端轮询、固定案例与部署入口。清单与语法检查不等同于每行语义审计。
未发现仓库内 AGENTS.md。未连接 ECS，不能断言 ECS 工作区干净或部署与本地一致。

确认的兼容边界：

1. POST incidents 当前已经返回 HTTP 202，但 `create_incident()` 同步 invoke 到
   END/interrupt；不能将该状态码视为已有持久化异步队列。
2. incident 元数据与 graph checkpoint 不在同一应用事务；首次 invoke 异常路径
   会删除元数据。保留现状，1A 才修改。
3. 创建时 thread_id=incident_id；读取使用 repository 保存的 thread_id。
   将来不得重命名旧线程、重建旧事件或通过读取触发 invoke。
4. `waiting_for_approval` 当前由 phase + approval_status 投影；恢复时仍需检查
   真正的 checkpoint tasks/interrupts，不能凭 API 字段自动批准。
5. 图支持仅采集、仅检索等裁剪配置；同一中间 phase 也可能已 END。
6. 原终态、审批与复查分别存储。GET rechecks 不采集；POST 才创建新观察且不幂等。
7. 旧 verification_succeeded 可能只有资源验证，缺省业务状态是 skipped。
8. 前端按 phase 轮询，manual_investigation 的 remediation_planned 仍在轮询集合中；
   后续应依据运行生命周期区分终态，本块不修改 UI。
9. Python 依赖多数为范围/未限定版本，只有部分精确 pin；没有完整 Python lock。
   不把 ECS 未读取的版本写成“已锁定”。后续锁定以 ECS 实测版本为依据。

## legacy phase → run.status 约定（设计映射，未写数据库）

优先保留原 phase。下表用于下一块设计与兼容读取；不为旧数据凭空生成租约、
队列或 run。`succeeded` 只代表任务完成，不代表业务健康。

| 旧 phase | 候选新 status | 必须核对的条件 |
| --- | --- | --- |
| created | queued / running | 只有未来事务已持久化 queued run 且未领取才为 queued；旧记录单独存在无法判断 |
| validated、collection_planned、evidence_collected、evidence_collected_with_errors、runbooks_retrieved、diagnosis_completed | running / succeeded | 有合法 pending 只读节点时为运行中；裁剪图正常 END 才为完成；无检查点不能补跑 |
| remediation_planned | succeeded / running | manual_investigation 且 requires_approval=false、图 END 为完成；待 prepare_approval 为运行中 |
| awaiting_approval | waiting_approval | pending 审批与真实 interrupt、事件/计划绑定一致；否则保留异常供核对 |
| approval_approved | running / reconciling | 核对检查点及外部写记录；旧版无操作账本，不能据此直接重放 PATCH |
| remediation_executed | running / succeeded / reconciling | 有确定写结果且待 verify 为运行中；裁剪图 END 为任务完成但恢复未验证；结果不明先核对 |
| remediation_skipped | succeeded | 图 END；unknown 诊断仍是 unknown，不能展示成已恢复 |
| approval_rejected | cancelled | 已保存拒绝记录、图 END；保留 rejected 原语义，不伪造主动取消 API |
| verification_succeeded | succeeded | 图 END，业务结论单独读取（旧字段缺失时 skipped） |
| verification_skipped | succeeded | 图 END，只代表流程结束，恢复未验证 |
| validation_failed、evidence_collection_failed、runbook_retrieval_failed、diagnosis_failed、remediation_failed、approval_failed、remediation_execution_failed、remediation_execution_conflict、verification_failed、failed | failed | 保存 errors/trace/action_result；结果不明写优先核对，不把失败当安全重试许可 |
| unknown、缺失、未识别值或状态冲突 | 不自动映射 | 保留原文，只读展示，人工核对；禁止默认 succeeded/queued |

waiting_user、retry_scheduled 无 legacy 对应值；不能从文本猜测。恢复协调器和
operation 账本尚未实现，本表不能直接作为自动恢复算法。

## 历史证据来源

| 能力 | 仓库来源 | 本轮含义 |
| --- | --- | --- |
| 十个固定案例 | evals/cases/v02-stage5/catalog.json、backend/tests/fault_cases、scripts/fault_cases/suite.py | 合成输入及程序断言；catalog 标识 7 项 live_supported，不等于十项真实实测 |
| 七个真实 facts 场景 | README.md「验收范围」、CHANGELOG.md | 维护者历史反馈，无本次新增原始现场测量 |
| selector/readiness 审批恢复 | docs/STAGE6_1_ACCEPTANCE.md、backend/tests/agent/test_graph_human_approval.py、test_graph_execution.py | 历史说明与 fake/in-memory 回归入口；不证明本次 PG 重启或受限身份 |
| 只读复查持久化 | docs/stage2-readonly-rechecks.md 2026-09-15 反馈、backend/tests/api/test_rechecks.py | 历史反馈称重启后仍在；本次需真实旧记录 GET 验证 |
| 已知旧失败 | docs/STAGE6_1_ACCEPTANCE.md「已有基线问题」 | 曾记录 execution_policy 4 项失败；本机缺 pytest，尚未复现或确认已修复，不删除测试、不放宽策略 |

## 本地执行与结果

原始输出见本目录 `0A-local/commands.json`，版本和基线清单见
`0A-local/inventory.json`；证据 SHA-256 见 `0A-local/SHA256SUMS`。

- `python -m unittest backend.tests.compatibility.test_stage0a_samples -v`：6 项通过。
  覆盖三份合成样本可读、审批绑定变化、旧资源验证默认值、复查禁止写入/模型声明，
  以及旧十案例 expected_facts 的逐项比较（10 个 subtest）；未运行图与完整案例 runner。
  十案例事实比较的分项记录另见 `0A-local/facts-only.json`。
- `python -m scripts.audit_stage0a`：基线 Python AST 解析通过；这是静态检查。
- 固定案例、审批/复查/持久化定向 pytest：已尝试，缺少 pytest，未收集测试。
- `npm test`、`npm run build`（frontend 目录）：已尝试，系统无 npm，未启动。
- 新增两个 Python 文件 AST 解析通过；尝试用 Git Bash `bash -n` 检查 ECS 命令，
  Bash 在本机启动失败（Windows signal pipe / Win32 error 5），未完成 Shell 语法实测。
- Python=3.13.11，不满足项目 >=3.12,<3.13；pydantic=2.12.4。
  LangGraph/checkpointer/psycopg 未安装；本机无 Node/npm/docker/psql。

未安装/升级全局环境来掩盖基线差异；没有用 Python 3.13 的样本校验冒充受支持运行环境回归。

## ECS 准确验收命令

以下在 **ECS 现有仓库根目录，Bash** 执行。先检查再同步，禁止 reset --hard、覆盖
未提交文件或删除卷。证据目录放仓库外，权限 700；实际输出可能含日志，不自动提交。

```bash
set -euo pipefail
umask 077
AUDIT_DIR=$(mktemp -d "$HOME/k8s-0a-XXXXXXXX")
export AUDIT_DIR
git rev-parse HEAD | tee "$AUDIT_DIR/head-before.txt"
git status --short | tee "$AUDIT_DIR/status-before.txt"
git diff --stat | tee "$AUDIT_DIR/diff-stat-before.txt"
# 如有修改，先核对并保留，停止同步；干净后拉取已推送的本块提交：
test -z "$(git status --porcelain)"
git fetch origin
if git show-ref --verify --quiet refs/heads/feature/baseline-contracts; then
  git switch feature/baseline-contracts
else
  git switch --track origin/feature/baseline-contracts
fi
git pull --ff-only origin feature/baseline-contracts
git rev-parse HEAD | tee "$AUDIT_DIR/head-after.txt"
```

在本块提交推送后执行以上命令。ECS 原 SHA 不同于基线时，
先审查 `git log --oneline 67f310d..HEAD`，不能把差异当成本块引入。

在现有 Python 3.12 开发虚拟环境执行，先确认版本、依赖已安装。
不要直接向运行中的容器 pip install。缺依赖应另建测试 venv 并记录安装后的版本；
当前范围依赖的重新解析不保证与在运行镜像一致。

```bash
set -euo pipefail
python --version
python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Use the existing Python 3.12 environment"'
python -m pip check
python -m scripts.audit_stage0a > "$AUDIT_DIR/inventory.json"
python -m pip list --format=json > "$AUDIT_DIR/python-packages.json"
set -o pipefail
python -m unittest backend.tests.compatibility.test_stage0a_samples -v 2>&1 | tee "$AUDIT_DIR/compatibility.txt"
python -m pytest backend/tests/fault_cases -q 2>&1 | tee "$AUDIT_DIR/fault-cases.txt"
python -m pytest backend/tests/agent/test_graph_human_approval.py backend/tests/agent/test_graph_execution.py backend/tests/agent/test_execution_policy.py backend/tests/api/test_incident_approval_service.py backend/tests/api/test_persistent_incident_service.py backend/tests/api/test_rechecks.py backend/tests/persistence -q 2>&1 | tee "$AUDIT_DIR/persistence-approval.txt"
(cd frontend && node --version && npm --version) | tee "$AUDIT_DIR/node-version.txt"
# 仅在缺少 node_modules 时，按已有 lock 安装开发依赖：
(cd frontend && npm ci) 2>&1 | tee "$AUDIT_DIR/npm-ci.txt"
(cd frontend && npm test) 2>&1 | tee "$AUDIT_DIR/frontend-test.txt"
(cd frontend && npm run build) 2>&1 | tee "$AUDIT_DIR/frontend-build.txt"
```

逐条检查退出码；非零即记录失败，不以最后一条成功代表整组成功。pytest 不连接
真实数据库；以上不运行故障注入、不创建真实事件、不批准旧事件。

对当前 Compose 部署读取**运行实例**版本与实际 schema，不执行 setup/迁移：

```bash
set -euo pipefail
BACKEND_ID=$(docker ps -q --filter label=com.docker.compose.project=k8s-incident-agent --filter label=com.docker.compose.service=backend)
PG_ID=$(docker ps -q --filter label=com.docker.compose.project=k8s-incident-agent --filter label=com.docker.compose.service=postgres)
test "$(printf '%s\n' "$BACKEND_ID" | grep -c .)" -eq 1
test "$(printf '%s\n' "$PG_ID" | grep -c .)" -eq 1
# 若任一 test 失败，停止并确认实际部署；不选择任意其他容器。
docker exec "$BACKEND_ID" python -c 'import sys,importlib.metadata as m; print(sys.version); print({p:m.version(p) for p in ["langgraph","langgraph-checkpoint","langgraph-checkpoint-postgres","psycopg","pydantic"]})' | tee "$AUDIT_DIR/runtime-versions.txt"
docker exec -i "$PG_ID" sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' | tee "$AUDIT_DIR/database.txt"
BEGIN READ ONLY;
SHOW server_version;
SELECT version, name FROM incident_agent_app.schema_migrations ORDER BY version;
SELECT to_regclass('public.checkpoint_migrations') AS checkpoint_migrations;
SELECT v FROM public.checkpoint_migrations ORDER BY v;
SELECT phase, count(*) FROM incident_agent_app.incidents GROUP BY phase ORDER BY phase;
SELECT count(*) AS recheck_count FROM incident_agent_app.rechecks;
ROLLBACK;
SQL
```

若数据库沿用独立 `infra/postgres/compose.yaml`，以上 PG_ID 可能为空，应先用
`docker ps --format '{{.ID}} {{.Names}}'` 确认并手动设置 PG_ID 后再执行只读 SQL。
应用 migration 预期为 1、2；checkpointer 有独立迁移编号，不应与应用版本比较。
表不存在、版本不符均记录为差异，不运行 init_persistence 修饰现场。

保存一份**真实旧审批、真实旧终态和已有复查历史**（输入现有 ID，不创建新事件）：

```bash
set -euo pipefail
read -r -p '旧待审批事件 ID: ' APPROVAL_ID
read -r -p '旧终态且有复查历史的事件 ID: ' TERMINAL_ID
[[ "$APPROVAL_ID" =~ ^[a-zA-Z0-9-]+$ ]] && [[ "$TERMINAL_ID" =~ ^[a-zA-Z0-9-]+$ ]]
# 校验失败停止。API 默认监听本机 8000；反向代理部署按实际只读地址设置。
API_BASE=http://127.0.0.1:8000/api/v1
curl --fail --silent --show-error --max-time 30 "$API_BASE/incidents/$APPROVAL_ID" > "$AUDIT_DIR/approval.json"
curl --fail --silent --show-error --max-time 30 "$API_BASE/incidents/$TERMINAL_ID" > "$AUDIT_DIR/terminal.json"
curl --fail --silent --show-error --max-time 30 "$API_BASE/incidents/$TERMINAL_ID/rechecks?limit=50" > "$AUDIT_DIR/rechecks.json"
python - <<'PY'
import json, os
from pathlib import Path
from backend.app.api.schemas import IncidentStatusResponse
from backend.app.services.recheck_service import RecheckResult, TERMINAL_PHASES
p = Path(os.environ['AUDIT_DIR'])
a = IncidentStatusResponse.model_validate_json((p/'approval.json').read_text())
t = IncidentStatusResponse.model_validate_json((p/'terminal.json').read_text())
assert a.waiting_for_approval and a.phase == 'awaiting_approval'
assert a.approval_status == 'pending' and a.approval_request is not None
assert a.approval_request.incident_id == a.incident_id
assert a.approval_record is None and a.approved is not True
assert not t.waiting_for_approval
assert t.phase in TERMINAL_PHASES or (t.phase == 'remediation_planned' and t.remediation_plan is not None and t.remediation_plan.action == 'manual_investigation' and not t.requires_approval)
rows = json.loads((p/'rechecks.json').read_text())['items']
assert rows, 'No existing recheck: record this as missing baseline evidence.'
for row in rows:
    r = RecheckResult.model_validate(row)
    assert r.incident_id == t.incident_id
print('PASS: existing approval, terminal and recheck records are readable; no resume or write performed.')
PY
sha256sum "$AUDIT_DIR"/*.json "$AUDIT_DIR"/*.txt > "$AUDIT_DIR/SHA256SUMS"
```

没有可用旧事件时标记“真实样本缺失”，不能用合成样本代替；不在 0A 为制造样本
注入故障。上述私有文件未经人工脱敏不进入 Git。需归档共享时保留原始私有 hash，
对 ID、操作者、日志及地址进行一致脱敏，并另记脱敏后的 hash 和来源。

## 配置、依赖、迁移、重启与退出条件

- 应用配置/依赖文件/数据库 schema：无修改；无新增生产依赖、迁移或重启要求。
- 新脚本 inventory 仅用 Python 标准库；兼容测试使用项目已有 Pydantic。
- 无需重建 Docker 镜像或运行 compose_up、init_persistence、index_runbooks。
- Git 仅同步本块源码/文档/合成样本与脱敏本地结果；不提交 .env、kubeconfig、现场日志。
- 未验证：Python 3.12 下全部最小回归、前端测试/构建、真实 ECS 工作区和运行版本、
  实际迁移及三类历史样本、PostgresSaver 重启读取；L2–L5 均未执行。
- 0A 退出：ECS 差异明确、三类真实样本可读且私有存档、要求的回归结果和失败有记录。
  有历史失败需明确归属与后续处置；任何新增安全回归阻断推进。
  收到本块验收反馈后再决定 0B；此文不声明依赖已冻结或恢复功能已实现。
