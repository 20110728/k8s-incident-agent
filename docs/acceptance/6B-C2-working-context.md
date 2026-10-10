# 6B-C2：调查状态与上下文

状态：C2 第二次浏览器复验仍失败；当前以文末 v2.2 收尾修复为准，待 ECS 验收。下方 v2/v2.1 为历史记录。

## 实现机制

- `working_context.py` 生成 `investigation-context-v2`。调查和收尾诊断共用程序选材；卡片只发送字段表示或解析失败摘录中的一种，避免原文与提取结果重复。短视图/扩展视图都是完整 JSON，数组和文本截短带遗漏标记；原记录不改写。独立的解释/比较接口保持原历史快照逻辑，本块先接入调查主路径。
- 当前状态引用现有 `policy_facts` 和证据编号，另记失败、身份冲突、待回答槽位和历史证据编号，不复制大正文。由持久化原记录确定性重建，调用账本保存该次状态及来源指纹；不新增事实表或复制一份完整 checkpoint。已有采样失效规则继续使失败复采后的健康判定变 unknown；补充 namespace/UID 与采样时间检查，较旧采样不覆盖较新观察，不同 UID 保留冲突。旧记录无时间/身份时保留既有顺序与 unknown 元数据，不能补造身份或采样时间。
- 人工回答沿用问题 ID/槽位/消息 ID 的绑定；每条送入最多 600 字符，超长保留首尾并标中间省略，原文仍保留。同一消息不再同时出现在回答和历史消息中。历史最多 5 条各 200 字符；旧结论标背景/假设。人工说“修好了”不会变成工具事实，等待人工后仍禁止据旧基线确认健康或生成写计划。
- 必需事实与已执行调查的卡片先于可选背景装入；仅已显示证据可引用，重复 ID/相同记录仅呈现一次，重复 ID/不同内容拒绝。记录选材版本、用途、摘要指纹、入选原因及遗漏 ID。必需最小输入放不下时，在模型调用前停止并持久化 `context_assembly_failures`，既有只读导出可看到；不降低引用校验、不付费让模型猜。
- 正常仍为一个调查模型，无摘要/分类模型，无新增预算。相同请求从账本复用结果；版本/输入变化仍拒绝付费重放。已保存原始证据和旧结果可读；不承诺旧版本活动调查跨本次升级继续运行。按需补读和动态合法动作目录留在 C3，因此浏览器上下文缺失问题尚不能宣称全面解决。

## ECS 验收与部署

先让活动调查完成，或在页面停止等待补充的调查；不要为了升级批准写操作。项目目录、Python 3.12 `.venv` 激活后：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6bc2.sh
```

预期 `PASS: 6B-C2 ECS acceptance.`，无失败/错误/skipped。受控模型/集群、真实隔离 PostgreSQL；覆盖卡片、上下文、预算、调查循环、追问恢复及生产审批衔接，不运行真实模型或前端构建。结果目录打印在末尾，重点文件为 `backend.txt`、`junit.xml`、`working-context.json`，沿用成功测试子库清理机制。

通过后复用已有后端部署脚本（无需构建前端）：

```bash
bash scripts/deploy_stage6b2b_backend.sh
```

预期 readyz 返回 ready；backend/worker 的 execution_mode 均为 queued，investigation_enabled 应保持 true。无新增配置、依赖或数据库迁移；需要更新并重启 backend 和 worker。新事件才使用新上下文；旧终态事件不用重跑。

可选对新事件使用 `python -m scripts.export_investigation_debug --incident-id <事件ID>`，查看 `metadata.context_version/selection/working_state` 或 `context_assembly_failures`。实际 Token 降幅和真实模型调查质量待实测，不以受控 PASS 推断浏览器全部通过。

本地仅完成 261 个 Python 文件 AST 语法解析、Bash `-n` 和 Git 差异检查；未运行 pytest、数据库动态测试或模型调用。

## C2 修复块（2026-10-10）

事件 `9ae60127-e9e1-49b3-9212-cf913e277e4a` 第一次生成正常、两次日志读取成功，第二次调用前因 Deployment/BusinessCheck 未装入停止；不是 6 万总额度用尽。导出另显示 `b'...\\n...'` 被当成一行，导致日志次数和时间提取失效。

- `tools/log_text.py` 在调查及基线日志入口统一 UTF-8 严格解码；坏编码/未知响应类型按原异常路径报告失败，不伪造可用日志。调查日志先解码、再脱敏、再限长，保存普通文本 payload，避免截断 JSON 字符串破坏换行。旧账本不改写；疑似 bytes repr 的旧记录标未解析，不 eval 或自动反转义。
- 模型视图升级 `investigation-context-v2.1`：移除重复名称/namespace/UID及部分展示字段，Endpoint 只取关系与就绪字段，日志只取消息、次数、首末时间和遗漏计数；C1 导出仍保留完整卡片。Pod 容器的当前状态与 `historical_only` 明确分开，提示过去退出记录不能证明当前根因，调查上个实例应选择 previous 日志。权限、引用、8k 单次估算与 60k 总额度不放宽，不新增模型调用。
- `test_c2_fix.py` 用同类真实字段、较长资源名和中文 bytes 日志验证两次采集后必需证据仍可见、100 行聚合、实际 JSONB/checkpoint 回放不重复调用、坏编码与脱敏限长。验收额外覆盖两个日志工具入口；`c2-fix.json` 记录估算和结果。此为受控回归，真实模型是否正确选择日志实例仍需浏览器复验。

沿用上面的 `accept_stage6bc2.sh` 和后端部署脚本；先结束活动调查再升级，用新事件验证，旧事件不重跑。无新增依赖/配置/迁移，无前端更新；需重启 backend/worker。本修复块本地仅完成 263 个 Python 文件 AST、Bash `-n` 与差异检查，未本地运行动态测试。

## C2 收尾修复：v2.2（2026-10-10）

- 日志调用使用 `_preload_content=False`，直接读取原始 HTTP 字节后严格 UTF-8 解码，读取上限 256 KiB，成功/异常均关闭响应并释放连接，避免 SDK 先转换成 `b'...'` 字符串。现有脱敏、保存限长和只读权限不变。
- 事件等结构化响应增加 `payload_complete`；完整 payload 可以解析，`coverage=partial` 仍只表示局部采样，不提升为完整健康证明。旧截断文本不猜测还原。
- 调查输入/输出上限改为 16,000/3,000；通用预算入口为 24,000/4,000；新运行总额 120,000，版本 `investigation-budget-v3`。模型结果保存上限同步翻倍，旧预算快照不改写，调查轮数/工具次数不增加。
- 上下文版本 `investigation-context-v2.2`：正常视图放不下必需证据时自动重装一次紧凑视图，保留引用、采样范围和字段省略标记；不增加 LLM 调用。收尾不发送采集工具目录。仍不足则保守交接，诊断页面保留已采证据数量及资源/业务状态，不编造根因。
- `test_c2_robustness.py` 覆盖真实 SDK 的模拟 HTTP 响应路径、坏编码释放连接、三轮六次采集、一次纠正、最终收尾和无重复回放；`c2-robustness.json` 为验收必需报告。另覆盖紧凑视图降级和新预算边界。仅 ECS 运行动态测试，本地只做静态检查。
- 命令不变：`bash scripts/accept_stage6bc2.sh` → PASS 后 `bash scripts/deploy_stage6b2b_backend.sh` → 浏览器创建一个新异常事件，确认能进入诊断/有事实说明的交接。无新增配置、依赖、迁移或前端构建；backend 和 worker 均需重启。真实模型与集群效果待该次浏览器验证。

### 精简排障导出

`python -m scripts.export_investigation_debug --incident-id <事件ID> --brief`，可加 `--run-id` 指定轮次。生成 `evals/results/investigation-debug/*-brief.json`，优先反馈此文件。保留停止原因、预算/实际用量、决策与工具轨迹、校验错误、上下文遗漏 ID、证据解析状态和少量日志/事件示例；删除完整正文、选材哈希及重复目录。数量/文本有上限，遗漏显式标记，缺失 usage 不当作零消耗。只读保存记录，不调用模型/集群、不修改数据库，无重启要求。`--brief` 自动包含精简证据信息，优先于 `--evidence-cards`；完整导出旧命令仍可用。
