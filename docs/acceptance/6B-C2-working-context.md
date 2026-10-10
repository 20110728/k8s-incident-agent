# 6B-C2：先跑通调查与诊断

本轮为单个修复验收块；当前规则覆盖此前 v2.x 的压缩与预算方案，旧方案可查 Git 历史。真实模型效果待 ECS 新事件验证，不实施 C3 或 Runbook 技能化。

## 当前机制

| 项目 | 新运行行为 |
| --- | --- |
| 调查决策 | 原 3 轮改为最多 5 轮；一轮可选多个工具，追问也占调查决策轮次 |
| 模型调用 | 仍最多 6 次，含失败和纠正；第 6 次只允许结论或停止，因此有纠正/追问时可能不足 5 轮采集 |
| Token / 总耗时 | 不再按输入估算、输出 token、累计 token、活动总时间、工具总数拒绝；实际用量仍记账，缺失 usage 不当作零消耗 |
| 模型输入 | 全部已保存的当前有效证据正文、已有人工回答、调查历史、已检索 Runbook 正文，经脱敏后传入，不按长度筛选或截取；新调查轮历史快照不截正文 |
| 日志及结果 | 不发送 tail_lines / limit_bytes，读取服务端仍保留的该容器实例日志；不截响应字节和保存正文，不限事件 50 条、结果 12K 字符；结构化诊断与工具批次不设长度上限 |
| 保留边界 | 服务/资源 UID、权限、引用、复采依据、人工问答协议、审批、幂等；单个网络请求超时及恢复观察窗口保留，它们不是整轮成本预算 |

实现：`runtime/budget.py` 用 `None` 表示无限额，仅调用数和决策轮数阻止继续调查；`investigation/model.py` 按调用次数决定末次收尾，SDK 重试为 0。`context.py` / `working_context.py` 使用 `investigation-context-v3-full`，原始正文只放一次；当前、历史、人工报告仍区分。`tools/log_text.py` 完整读取并关闭 HTTP 响应，工具执行前后仍检查身份。预算 API / 前端支持 null 显示“不限”，页面显示全部调查步骤。

不能取消模型/Embedding 服务自己的上下文、输出容量和日志轮转限制。Kubernetes 字段、API 表单和人工回答协议的合法性限制不属于本次扩展；独立解释/比较及旧工作流仍用原上下文构造器。精简排障导出继续限长，避免反馈文件膨胀。旧预算快照不改写，旧终态事件不自动重跑。

诊断语义不合规且纠正耗尽时，沿用程序 unknown 报告：保留事实和缺失信息，不编造根因、不生成写计划。权限、身份、引用错误不被该分支掩盖。

## ECS 验收与部署

项目目录、Python 3.12 `.venv` 激活后：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6bc2.sh
(cd frontend && npm ci && npm test -- src/features/incidents/budget.test.tsx)
```

预期后端 `PASS: 6B-C2 ECS acceptance.`，无失败/错误/skipped；前端测试通过。后端采用受控模型/集群与真实隔离 PostgreSQL。`count-only.json` 验证 5 轮采集、第 6 次收尾和回放；`raw-evidence.json` 验证初次请求 50 行仍接收全部 1500 行。另覆盖超旧 240KB 日志、长结构化结果、无成本阻断、历史正文和旧预算兼容。

通过后先结束活动调查，再部署：

```bash
bash scripts/deploy_stage6b2b_backend.sh
bash scripts/deploy_stage4c_frontend.sh
```

预期后端 ready、backend/worker 为 queued 且 investigation_enabled=true，前端健康检查成功。无新增 `.env` 配置、依赖或数据库迁移；backend/worker 需重建或重启，前端需重新构建。部署后创建新事件，检查调查、诊断及预算“不限”；unknown 是合法的证据不足报告，不代表根因确定或服务恢复。

失败时提供精简文件：

```bash
python -m scripts.export_investigation_debug --incident-id <事件ID> --brief
```

本地：268 个 Python 文件 AST 与 `git diff --check` 通过；按约定未运行 pytest、数据库、模型或前端测试/构建，本地无前端依赖。ECS、真实模型/集群及浏览器结果待验证。

本次验收用例修正：配置分类冲突用例补充合法 Runbook 引用，仅测试语义纠正；人工变更复采用例在一次合法复采后主动结束；预算用例分别验证无限额与旧数值上限，已用时间保持数字。模拟模型断言直接报告测试失败，不再被包装为供应商请求失败。仅修改测试与本文，无运行时代码变更；拉取后重跑 `bash scripts/accept_stage6bc2.sh`，本补丁无需迁移、重启或前端更新。
