# 收尾材料核对（2026-10-12）

核对基线 `c01615c`，本轮仅整理文档，不修改程序、删测试或重跑动态验收。

## 当前入口

- 总览：根 README；流程：`docs/CODE_READING.md`。
- 当前范围：主开发计划顶部及第 14 节、证据与报告分块计划。
- 核心回归：`scripts/accept_stage6bc2.sh`；恢复导出：`scripts/export_remediation_debug.py`。
- 最新反馈：多轮 selector 完成受控修复与连续 3/3 观察；dependency 已核对关键证据入模。readiness 最后实测、恢复导出及最新回归待反馈。

## 盘点与处理

| 材料 | 判断与处理 |
| --- | --- |
| `evals/results/local/4b1/static-checks.txt`（261 字节） | 旧本地静态结果，不是当前验收证据；已被 gitignore 排除，列为可清理候选，本次未删除 |
| `evals/results/transfers/stage3a-f5174fe.bundle`（13846 字节） | 旧 Git 传输包；不参与运行。未确认其备份用途，不删除 |
| `evals/results/ui-proposal/incident-workspace.html`（18509 字节） | 旧页面提案，不是当前前端源码；不参与交付，保留本地参考 |
| 根 `debug.log`（2210 字节） | 已忽略的本地日志，不作为验收依据；来源与保留需求未确认，不读取内容或删除 |
| `docs/acceptance/0A-local/*` | 基线审计结果，被 0A 文档和 SHA256SUMS 引用；保留原内容与路径 |
| `evals/compatibility/`、`evals/cases/` | 兼容样本、固定输入和评测契约；不是中间结果，保留 |
| `evals/manual-results/` | 历史单轮样例；不据此证明当前多轮效果，保留历史用途 |
| `backend/tests/` 中的 stage/day 测试及 `run_*_acceptance.py` | 不因阶段名删除。验收 runner 仍引用这些目录，生产流程/调查测试还导入旧测试夹具；未找到足以安全删除的整组测试 |
| `scripts/accept_stage*.sh` 与 `docs/acceptance/` | 分层验收、恢复与权限审计入口，保留。6B-C2 不替代所有早期恢复/权限测试 |
| day14/day15、stage1/stage2、STAGE6_1 文档 | 加历史标识，不作为当前功能清单；路径保留以避免破坏旧链接与基线清单 |
| 主计划第 0–8 阶段细则 | 保留最初设计，顶部和当前入口明确实际范围；复杂对照评测不标已完成 |

没有仅凭“无人引用”判断 pytest 测试无用；测试还可能由目录发现。本次不为减少文件数量删除失败样例、权限反例、兼容夹具或原始审计。

## 收尾条件

1. readiness 实际参数与登记值一致，经审批执行，资源与业务连续观察通过；保存独立诊断和恢复导出。
2. 最新核心回归与恢复导出在 ECS 验证；未覆盖项明确保留，不将历史 PASS 当作本次运行。
3. 固定交付提交、运行配置和演示说明。unknown 报告增强、检索调整及复杂公平评测延期。

本轮文档与链接静态检查不需要配置、依赖、迁移、部署或重启；不影响进行中的 readiness 实测。
