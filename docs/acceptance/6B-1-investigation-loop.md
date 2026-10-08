# 6B-1：有界只读调查图

状态：维护者已反馈 `PASS: 6B-1 ECS acceptance.`。6A 已由维护者反馈全部通过、无 skipped，迁移重启完成。

本块交付独立调查图及回放验收入口。现有 HTTP、worker 路由和浏览器仍使用原流程；不会因为 pull 就自动切换事件处理。先证明调查、记账、恢复机制，再在 6B-2 接业务入口。

## 1. 分块及目标

| 验收块 | 内容 | 当前状态 |
| --- | --- | --- |
| 6B-1 | 模型选择只读工具、证据更新、停止、持久化调用结果、断点回放 | ECS 已通过 |
| 6B-2a | 追问 interrupt/resume、回答幂等、有理由复采核心 | 已实现待验收，见 `6B-2a-human-resampling.md` |
| 6B-2b | 正式业务路由/worker 版本分流、回答 API 适配、程序组装白名单计划与原审批链衔接 | 待开发 |
| 6B-3 | 页面解释调查过程，真实模型/集群小规模对照，耗时和调用开销记录 | 待开发 |

共同边界：固定目标和权限，动态选择调查路径，实际写入必须走原审批与执行校验。一个调查模型承担下一步选择与最终诊断，不增加分工 Agent、摘要模型或每轮 RAG。

## 2. 核心机制如何实现

### A. 怎样选择有用的下一步

**大白话：** 先告诉模型“现在知道什么、还可以查什么”。模型说明缺什么、为什么值得查，然后从可用工具里选。程序核对它的选择能不能执行。

**实现：** `backend/app/investigation/contracts.py` 定义按 action 区分的 Pydantic 联合结构：

- `collect` 只提交 `missing_fact/reason/evidence_ids/requests`，无需先生成整份诊断。每批 1–2 个工具，顺序执行；模型被要求只组合不相互依赖的查询。
- `conclude` 提交完整 `CurrentDiagnosis`；直接作为最终结果，不另调一个模型重写总结。
- `ask_user/propose_plan` 保留明确输出结构，本块只返回交接，不执行追问恢复或修复。
- `stop` 给出停止原因、依据和未解决事项。

`context.py::build_context` 打包事实、证据摘录、工具用途、资源引用、最近决策及一次校验反馈。先放必须引用的业务/配置/资源事实，再放补充观察；只允许引用实际进入上下文的 ID。逐条控制大小，保持整个请求为合法 JSON，超限交接，不把半截 JSON 当结构化证据。摘录可以截断，但有明确标记。

`tools/investigation.py` 从登记 profile 和采集到的 UID/归属构建 `resource_ref`。模型不能传 namespace、URL、shell、label 扫描等任意参数。`graph.py::validate_decision` 先校验整批 Schema、引用和工具/对象匹配，再执行第一项；每次读取前后复核身份。权限拒绝保留未知结果，模型可选择仍有意义的其他允许工具；对象替换则立即结束本轮。

**自主性边界：** 程序不根据错误类型替模型选日志或事件；由模型选择。程序目前能验证权限、引用和硬规则，不能数学证明模型所说的“值得查”确实有价值，也不能自动证明两次查询语义独立；这部分需要 6B-3 实测。

### B. 新证据怎样真正改变判断

**大白话：** 每次查到的东西都留底，再整理一份“现在可以用的证据”给模型。旧记录仍可追溯，但新的失败不能被旧的成功盖过去。

**实现：** `evidence.py::adapt` 给每次工具观察分配由请求 ID 派生的稳定证据 ID，保存时间、来源、coverage、截断及错误信息。`baseline` 和 `observations` 保留历史；`current_state/active_evidence` 生成当前视图，不修改历史。

- 同一个对象的新结构化概要替换对应旧事实；失败或不完整概要让该部分成为未知。
- 业务复读失败、空结果或部分缺失会先使旧业务结果失效，不能继续拿旧 passed 宣告正常。
- 当前日志和 previous 日志分开；同一容器当前日志复读失败会使旧当前日志失效。previous 不能证明当前故障。
- 单份事件、单个 EndpointSlice/ReplicaSet 只作为补充观察，不冒充完整资源清单。日志尾部永远标为部分覆盖。
- 初始重复证据不会被静默合并成“可信的一条”；已有事实校验仍能识别歧义。

`graph.py` 每轮重新计算 `diagnostic_facts`，使用原 `validate_diagnosis_references` 和 `validate_diagnosis_assessment` 核对结论：顶层及症状/假设引用必须在本轮上下文中；资源/业务状态必须匹配事实；依赖问题需要当前失败症状及当前日志，根因仍标 suspected。校验无法保证任意自然语言的因果推断正确，因此保留未验证范围。

回放报告 `evidence-change.json` 展示“资源未就绪、业务未知 → 追加当前日志 → 带新引用的依赖问题假设”，并记录前后事实、调查理由和调用次数。这里模型是受控替身，用于证明新证据确实传入下一次判断；不等于已经证明真实模型会正确改判。

### C. 怎样停止，并防止无效循环和重复花费

**大白话：** 模型不能靠反复换个说法一直查。已经拿到的结果直接复用；不确定上次请求有没有完成，就报告未知，不偷偷重发。

**实现：** `runtime/budget.py::reserve` 在运行租约检查和数据库行锁内原子完成请求登记、指纹比较及预算预留。结果存入现有 `run_budgets.payload`；`model.py::call_model` / 工具 `call` 先保存结果，再让图进入下一节点。

| 情况 | 当前处理 |
| --- | --- |
| 同一 request_id，参数/上下文指纹相同，有已存结果 | 直接取结果；不再调用模型或工具，不重复计费 |
| 同一 request_id，但指纹不同 | `REQUEST_INPUT_CHANGED`，停止复用 |
| 已登记请求，没有持久化结果 | `REQUEST_OUTCOME_UNKNOWN`，保留预留额并交接 |
| 新 request_id，重复相同语义查询 | `DUPLICATE_TOOL_EVIDENCE`，不发第二次请求 |
| UID、归属、Deployment generation/profile 变化 | 结束本轮，不能把新对象当旧对象继续调查 |

语义查询指纹包含工具、namespace、资源 UID、日志容器及 previous；不包含全局证据 hash 和 tail_lines。因此无关证据变化、把 100 行改成 101 行不能绕过防重。**本块不自动允许“过了一会儿再查”**；用户确认变更、时效和新采样代次的判定在 6B-2 实现。

`graph.py` 使用 LangGraph 的 `initialize → decide → collect → decide` 条件边。`entrypoint.py` 装配带租约 fencing 的 PostgreSQL checkpointer；旧线程检查点不能直接作为新图恢复。调用结果账本解决“工具已返回，但图节点尚未保存”的窗口，checkpoint 负责恢复图位置；这两层各有职责。调用在结果落盘前崩溃仍可能已到达供应商，无法承诺外部请求恰好一次。

初始采集与 RAG 同样使用固定请求编号保存结果。已完成结果复用，未保存结果不自动重发。初始采集沿用现有资源/归属/profile/业务采集，`include_details=False` 省去自动日志和事件；保留原有 Service 选 Pod/命名空间回退行为，不宣称已重写成新的全集群发现器。旧流程默认 `include_details=True`。

## 3. 调用和预算约束

| 项目 | 6B-1 限制 |
| --- | --- |
| 调查决策 | 最多 3 次；结构纠错仍属原决策，但单独算模型尝试 |
| 追加工具 | 最多 6 次，每批最多 2 次，串行执行 |
| 生成模型实际尝试 | 最多 5 次，包含纠错和最终总结；SDK 重试为 0 |
| 校验修正 | 全 run 共享最多 1 次；资源越界不协商放行 |
| 末轮 | 第 3 次采集后允许一次只 conclude/stop 的模型调用，不再取证；已 conclude 不追加总结 |
| 单次生成 | 估算输入上限 8k、输出参数 1.5k，调用前预留 9.5k token、30 秒 |
| run 总量 | 沿用 40k token、300 秒活动时间；90 秒约束明确用于追加工具活动 |
| 收尾余量 | 非最终模型调用/工具调用保留一份 9.5k token、30 秒空间；不足则程序交接 |

输入估算沿用 UTF-8 字节数/3，加 Schema 和 512 余量；不是 Qwen 精确 tokenizer。优先结算供应商 usage，缺失时保留预留额度，不宣称实际费用绝对上限。Embedding 单独沿用已有预算记账，不计入 5 次生成模型上限。预算不足时输出结构化已知/未知和下一步，不为“生成好看的总结”突破预算。

## 4. 文件和 ECS 验收

核心新增在 `backend/app/investigation/`：`contracts/context/model/graph/evidence/records/entrypoint.py`。兼容扩展在 `runtime/budget.py`、`tools/investigation.py`、`tools/evidence_collector.py`、`agent/dependencies.py`。回放测试在 `backend/tests/investigation_loop/test_loop.py`，并扩展原采集单测验证默认完整采集和精简采集。

在 ECS 项目目录、已激活的 Python 3.12 `.venv` 下执行：

```bash
git pull --ff-only origin feature/baseline-contracts
bash scripts/accept_stage6b1.sh
```

预期：pytest 全部通过、零 skipped，最后输出 `PASS: 6B-1 ECS acceptance.`。脚本建立隔离测试数据库，并检查 JUnit 和改判报告均生成；数据库保留方便排查，不改生产事件。结果目录 `evals/results/6b1/incident_agent_test_6b1_.../` 包含 `commit.txt/backend.txt/junit.xml/evidence-change.json`。失败请提供该目录的 `backend.txt`，必要时再补 `junit.xml`。

覆盖：不同证据选择不同受控路径、新日志进入改判、足够证据直接结束、整批先验证、重复/越界拒绝、目标变更、权限失败、单次纠错、模型预算、未知 usage、并发请求登记、真实 PostgreSQL checkpoint 恢复不重复读取、截断上下文、旧成功失效、提示注入不能增加动作。

配置/依赖/迁移：不新增环境变量或依赖；复用 6A 的迁移 13，无新迁移。测试只需现有 PostgreSQL；受控模型和 Kubernetes 响应不访问真实供应商/集群。本块不要求重启前后端或 worker、不构建前端，也不改变 queued/sync 设置。

本地仅执行静态检查；维护者已反馈本块 ECS 脚本通过。真实模型结构化输出兼容性、路径质量、工具现场效果和浏览器展示尚未验证；追问恢复/合理复采核心进入 6B-2a，业务入口和计划衔接进入 6B-2b。
