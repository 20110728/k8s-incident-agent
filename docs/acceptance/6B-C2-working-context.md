# 6B-C2：日志采样、详情减负与模型排障

本次只修复调查流程及排障，不进入 C3 或 Runbook 技能化。

| 模块 | 当前实现 |
| --- | --- |
| 日志 | 基线和调查工具第一次就请求最近 1000 行、最多 256 KiB；本地再检查返回范围。保留窗口原文，不做模型摘要；达到边界标记部分采样。 |
| 调查额度 | 最多 5 轮决策、6 次模型调用（含失败/纠正）；追问占轮次。无累计 token、总时间和工具次数额度。权限、身份、引用、复采依据、审批、幂等及单次网络超时保留。 |
| 浏览器 | public_evidence.py 仅投影日志字段末尾 2000 字符，不修改保存证据或模型输入；旧事件同样生效。点击读取正文才通过事件/run 绑定的只读接口加载，不重新采集。 |
| 精简排障 | brief_debug.py 补充供应商异常类型、HTTP 状态、错误码、请求 ID、错误信息；保留规则拒绝原因。 |
| 最后一次模型调用 | model.py 调用前把 messages/schema 存入现有预算账本，仅保留最后一份输入；保存最终响应、结构化输出或异常。--last-llm 单独导出，不增加调用、不进入页面轮询。 |

上下文版本 investigation-context-v3.1-log-window；采样窗口原文直接入模，供应商容量限制仍存在。旧记录未保存的输入/输出明确为空，不重跑补造。导出不含 HTTP 密钥或隐藏推理。诊断语义不合规且纠正耗尽时沿用 unknown 报告，不编造根因或生成写计划。

## ECS 验收与部署

项目目录、激活 Python .venv 后：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6bc2.sh
(cd frontend && npm ci && npm test)
```

预期 PASS: 6B-C2 ECS acceptance.，前端测试通过。回归覆盖日志边界、旧大日志详情与按需正文、跨事件/run 拒绝、供应商错误、最后一次输入输出及重放不覆盖。脚本使用受控模型/集群与隔离 PostgreSQL，不代表真实供应商验收。

结束活动调查后部署：

```bash
bash scripts/deploy_stage6b2b_backend.sh
bash scripts/deploy_stage4c_frontend.sh
```

无新增配置、依赖、迁移。后端和 worker 均需更新，前端需构建更新。浏览器检查旧事件打开、证据预览、点击加载正文；再创建新事件验证真实调查和诊断。

```bash
python -m scripts.export_investigation_debug --incident-id <事件ID> --brief
python -m scripts.export_investigation_debug --incident-id <事件ID> --last-llm
```

分别生成 evals/results/investigation-debug/*-brief.json 与 *-last-llm.json；可加 --run-id <任务ID>。默认最新诊断任务；调用失败也导出已保存输入，未收到输出则为 null。

本地仅做 Python AST 和差异静态检查；未运行 pytest、数据库、模型或前端测试/构建。ECS 及浏览器验收待反馈。
