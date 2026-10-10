# 6B-C1：程序预处理

状态：已实现，待 ECS 验收。本块只落地程序提取及只读检查入口；C2 才切换调查上下文，当前浏览器 `REQUIRED_EVIDENCE_NOT_IN_CONTEXT` 不宣称已修复。

## 核心机制

- `investigation/cards.py` 提供 `evidence-card-v1`：从既有证据生成带 ID、原记录位置/指纹、UID/容器、时间、覆盖、错误、截断和解析状态的卡片。覆盖已有资源字段；未知类型、损坏结构和不完整工具 JSON 返回有界原文，不推断健康，不改写原始数据。缺失的旧元数据保持未知。
- 日志只移除可解析且带时区的时间前缀，再按原始消息精确分组；先分组再脱敏，避免不同凭证被脱敏后合并。保留次数、首末行号及时间，数字/业务标识不做模糊归并。最多扫描 120000 字符预算内的完整行、展示首尾共 8 组；遗漏行/组/出现次数明确记录。首末指原文顺序，不声称日志按时间排序。
- 字段列表最多 20 项、文本 600 字符、嵌套最多 5 层，限长记录路径及遗漏数；先脱敏再截断，命令参数及环境字段不导出。支持 PodSelection 的现有字段、容器资源 requests/limits、退出原因等。JSON 对象键排序，数组顺序保留，恢复后相同记录得到相同卡片。
- `export_investigation_debug.py --evidence-cards` 在既有只读事务内追加卡片，最多 100 条；保留不同时间的相同内容及重复 ID，使用记录位置区分，不冒充当前状态去重。读取终态快照的 baseline/observations，或预算中的旧 baseline；旧普通快照支持 evidence。运行中尚未发布的追加观察可能不可见，明确标注范围；不重建未保存内容、不调用模型/集群、不写新卡片表。
- 默认导出、现有 prompt、RAG、预算、审批和执行路径保持原行为；卡片留给 C2 统一选材接入。本块不做语义压缩和额外摘要调用。

## ECS 验收

在项目目录、Python 3.12 `.venv` 激活后执行：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6bc1.sh
```

预期 `PASS: 6B-C1 ECS acceptance.`，无失败/错误/skipped。仅运行卡片、导出及现有压缩预算相关回归，不跑前端或真实模型。沿用隔离 PostgreSQL 与通过用例测试库清理机制；失败库保留，父测试库保留。结果在打印目录的 `backend.txt`、`junit.xml`、`evidence-cards.json`、`database-lifecycle.jsonl`。

可选检查一个实际旧事件，无须重新调查：

```bash
python -m scripts.export_investigation_debug --incident-id <事件ID> --evidence-cards
# 指定轮次时增加：--run-id <runID>
```

预期打印 run_id 和 Saved 文件位置；JSON 新增 `evidence_cards`。有记录时可见来源、提取结果、日志次数和遗漏信息；无可读记录时为空，不自动补采。导出包含脱敏后的业务片段，分享前检查业务信息。

无新增配置、依赖、数据库迁移；本块宿主机 pull 后即可验收/导出，不要求重启 backend/worker 或构建前端。尚未验证：ECS 动态结果、实际旧事件导出；上下文降耗、浏览器调查和引用联动属于 C2/C3。

本地仅完成 258 个 Python 文件 AST 语法解析、验收脚本 Bash `-n` 和 Git 差异检查；未运行 pytest、数据库集成、真实模型或前端构建。
