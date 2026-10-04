# 0B：最小开发契约与评测冻结交付

- 日期：2026-10-04；基线 aa3b082。
- 范围：只交付 0B，不实现 1A/worker，也不扩大 0A 检查。
- 已承接维护者反馈：0A 回归通过、迁移 1/2 与运行版本明确；旧现场样本允许暂缓，
  不能记成通过。详见 `0A-ecs-feedback.md`。

## 改动文件

| 文件 | 用途 |
| --- | --- |
| docs/adr/0001-durable-runs.md | LangGraph/应用层职责；incident/run/thread；所有 run 状态进入退出；恢复与写未知分类；1A HTTP/数据/幂等/部署契约 |
| docs/adr/0002-evaluation-protocol.md | 数据划分、B0–B3 比较、预算、评分/阻断项、输入隔离与版本化 |
| evals/cases/complex-v1/ | C01–C12/R01–R06，18 份请求、18 份独立预期、4 份初始快照、catalog/budget/hash manifest |
| scripts/check_stage0b_contracts.py | 标准库静态校验，不导入应用，不访问模型/数据库/集群 |
| docs/acceptance/0A-ecs-feedback.md | 前一块的 ECS 反馈及用户批准暂缓项，未追加基线检查 |

## 本地执行及结论

按用户要求只做静态检查，没有运行 pytest/npm test 或真实故障注入。

命令：`python -m scripts.check_stage0b_contracts`

```text
PASS: 18 case contracts; 4 initial snapshots; 42 frozen JSON files
Scope: static only; full scenario runner, holdout inputs and live acceptance are pending.
```

另外 AST 解析新增 Python 脚本、`git diff --check` 通过。未更改 backend/app、
frontend、compose、依赖或数据库迁移；四个初始快照来自已有合成案例，非真实采集。

## ECS 只需执行

本块推送后，在仓库根目录运行（无需启动任何服务）：

```bash
git status --short
# 确认没有未提交修改后继续
git pull --ff-only origin feature/baseline-contracts
python -m scripts.check_stage0b_contracts
```

预期为上面的两行 PASS/scope 文本；退出码 0。文件缺失、被修改、案例不全、
输入夹入预期字段或来源变化时退出 1；错误 CLI 参数退出 2。
Python 标准库即可，无新增安装。**不用重跑前后端回归、不重启、不迁移、不调用模型。**

## 尚未验证及下一块

0B 只固定契约。完整 runner、留出输入、真实故障注入、并发事务、进程恢复、
真实模型评分、运行预算强制及 Agent 对 expected 的运行时访问隔离均未实现/未验收。
ECS 本块静态命令尚待维护者执行，不能把 0A 反馈当作本块运行证据。

下一块只实施 **1A：运行记录与请求幂等**：增量 migration 3、原子接受、按键找回、
兼容旧读取、事件/运行列表和 PostgreSQL 并发验收。worker 留给 1B；queued 模式
先隔离验收，现有 sync 演示继续使用，不把“可排队”宣传成“已后台执行”。
