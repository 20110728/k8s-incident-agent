# ADR 0001：可找回运行、兼容线程和保守恢复

日期：2026-10-04。状态：0B 开发契约；实现按验收块逐步开放。
代码基线：aa3b082；ECS 依赖快照见 `../acceptance/0A-ecs-feedback.md`。
本 ADR 不宣称下述表、API 或恢复协调器已经实现，不升级 LangGraph。

## 决策与取舍

保留 LangGraph 管理节点路由、共享状态、checkpoint 和 interrupt；应用层管理
HTTP 幂等、任务持久化、领取/租约、版本、取消和外部写核对。自建状态机可以减少
框架依赖，但需要重新实现并验证检查点和中断恢复；现有图与历史检查点已经依赖
LangGraph，当前收益不足以支持替换。LangGraph checkpoint 本身不是任务队列，
也不提供 PostgreSQL 与 Kubernetes 之间的事务。

## 身份、存储与兼容

- incident：长期事件，namespace/service_name 创建后不变；phase 继续保留旧语义。
- run：一次调查/恢复任务。run_id 与 incident_id 分离；新 run 使用新 thread_id；
  同一个 run 的 interrupt resume 复用同一个 thread_id。parent_run_id 关联重试/新轮次。
- 历史事件维持 incidents.thread_id 映射，允许没有 runs 行；GET 返回 run=null，
  不创建伪 run，不重命名历史 checkpoint，不从 phase 猜测写操作可以重放。
- evidence_set / plan_revision / approval_id 固定一轮证据和授权。新计划不能继承旧批准。
- rechecks 继续单独追加；messages 在 4A 实现；operations 在 2B 实现。
- 旧数据读取暂缓已由用户接受（找不到历史 ID）；在首次改动旧数据读取路径的 1A
  验收时补测或再次明确样本缺失。0B 不改写历史数据，不扩展 0A 基线检查。

1A 增量 migration 3：`incident_agent_app.runs`，不修改/回填旧 checkpoint：

| 字段组 | 冻结约束 |
| --- | --- |
| 身份 | run_id TEXT PK；incident_id FK；thread_id TEXT UNIQUE；run_kind=diagnosis（首版）；parent_run_id 可空 FK |
| 输入 | input_revision=1 起；input_payload JSONB 为规范化请求；input_sha256；workflow_version=incident-v1；接受后不可原地改写 |
| 幂等 | idempotency_scope、idempotency_key 可空成对；两列唯一；request_sha256；首个创建 run 保存键 |
| 生命周期 | status；created_at/updated_at 数据库时间；finished_at 可空；last_error 脱敏 JSON；attempt 默认 0 |
| 1B 租约 | lease_owner、lease_epoch 默认 0、lease_expires_at、heartbeat_at、next_retry_at；1A 不领取 |

活动状态为 queued/running/waiting_user/waiting_approval/retry_scheduled/reconciling。
按 incident_id 建上述状态的部分唯一索引，约束单事件活动 run 唯一；解释型任务首版
也排队。取消时如有外部写在途，先保留 reconciling 和取消请求，不提前释放活动锁。
人工交接可以结束 run，但未闭合 operation 是独立阻塞条件；后续创建/领取 run 必须
在同一事件事务锁内检查，未核对闭合前不得开始新写，不能仅靠活动 run 唯一索引。
所有更新均校验版本/有效拥有者；普通 GET 不更新 phase，不 invoke 图。
messages/operations 的具体 SQL 在其实现块增量迁移，不提前创建无使用者的表。

## 1A HTTP 契约（拟实现）

- `POST /api/v1/incidents` 请求体沿用当前 CreateIncidentRequest。
  `Idempotency-Key` 可选，1–128 个 ASCII `[A-Za-z0-9._:-]`；不合法返回 422。
  无键的两次 POST 是两个独立事件，不能承诺去重。
- 单人部署 scope 固定 `single-operator:create-incident:v1`；不是身份认证。
  将来加入认证必须按稳定主体标识分域，不能信任客户端自填 scope。
- 规范化：先走现有 Pydantic 请求校验/strip；再 `model_dump(mode='json')`；
  JSON UTF-8、ensure_ascii=False、sort_keys=True、separators=(',', ':')；SHA-256。
  不额外折叠 description 内部空白，不做 Unicode 归一化，省略字段按模型默认值处理。
- 同 scope/key 和同摘要返回原 incident_id/run_id（HTTP 202，可已完成）；
  同键不同摘要返回 409/IDEMPOTENCY_CONFLICT。保留键至关联事件显式归档清理，
  首版不自动过期、不提供删除接口，防止响应丢失后重复创建。
- 一个短事务写 incident + queued run；唯一约束解决并发键竞争；冲突回读赢家，
  不先调用图/LLM。事务失败返回 503，不能声称已接受。提交后丢响应仍可按键找回。
- 返回兼容 IncidentStatusResponse，增加可空 `run` 摘要，含 run_id/status/run_kind/
  created_at/updated_at/finished_at/attempt/last_error_code；queued 时 phase=created，
  request 可读，waiting_for_approval=false，其余字段沿用空/default；不把 run.status
  填入 legacy phase。未知字段兼容由前后端测试验证。
- `GET /api/v1/incidents/{id}`：已有 run 但尚无 checkpoint 时从持久化输入构建响应；
  旧事件走旧 thread 映射。已运行却找不到应存在的 checkpoint 时返回明确错误，
  不伪造 queued、不重建事件。
- `GET /api/v1/incidents?limit=20&cursor=...` 与
  `GET /api/v1/incidents/{id}/runs?limit=20&cursor=...`：上限 50；返回
  `{items, next_cursor}`，按 (created_at DESC, id DESC) 做 keyset 分页。
  cursor 是版本化 base64url JSON 时间/ID，校验结构和查询范围；无效 422。
  列表只给身份、目标、phase、时间和 run 摘要，不批量载入日志/checkpoint。
- `GET /api/v1/incidents/by-idempotency-key/{key}`：同一 scope 下按键找回，
  不存在 404；静态路由注册在 `/{id}` 前。返回同一 IncidentStatusResponse。
  键不是访问凭据，不允许公开多租户使用该未认证演示接口。
- 若浏览器携带 Idempotency-Key，CORS allow_headers 同步加入该字段。

1A 成立的四个必须项：同键并发只得到一个 incident/run；内容冲突 409；提交失败
不接受；无 checkpoint 的 queued 及旧事件可读。用真实 PostgreSQL 验证，不以 mock
证明事务/唯一约束。run 失败保留记录；queued 分支不得调用旧删除异常路径。

## 1A 与 1B 的可部署边界

1A 新增 `INCIDENT_AGENT_EXECUTION_MODE=sync|queued`（设计字段，当前尚不存在），
缺省 sync 保持既有演示；queued 仅用于隔离数据库/独立端口的 1A 验收。
sync 不宣传请求幂等，收到 Idempotency-Key 明确返回 409/QUEUED_MODE_REQUIRED，
防止调用者误以为已经去重。queued 才实现上述原子接受契约。
1A 响应另给 execution_mode/worker_available=false 等能力说明，不能显示“正在诊断”。
1B worker 通过验收后统一切 queued，才发布异步执行体验；不以 FastAPI BackgroundTasks
冒充 durable worker，不静默丢弃已排队任务。

## 状态进入、退出及证据

下列转换均为设计，状态变化与证据引用持久化；不得只靠前端按钮或进程内锁。

| 状态 | 合法进入条件 | 合法退出和 guard | 保留证据 |
| --- | --- | --- | --- |
| queued | 创建/显式新一轮事务提交，输入已保存 | running：原子领取并获得新 epoch；cancelled：未发外部写的显式取消 | 输入摘要、幂等键、请求与提交时间 |
| running | queued/retry_scheduled 领取，或检查点分类后受控续跑 | waiting_user/approval：实际 interrupt；retry_scheduled：可重试只读错误且预算未尽；reconciling：写结果未知；succeeded/failed：真实 END 分类；cancelled：无在途写 | thread、checkpoint ID、pending tasks、epoch、attempt、调用成本 |
| waiting_user | 持久化 question_id/revision 与 interrupt | queued：匹配问题/版本的已保存回答，再领取后 resume；cancelled：无在途写；错误版本不变并 409 | 原问题、消息、证据版本和恢复输入 |
| waiting_approval | 持久化 plan_revision/approval_id 与 interrupt | queued：匹配版本的批准已事务保存；cancelled：拒绝/取消且无在途写；过期/冲突不转移并 409 | 原计划、审批绑定、决定、操作者来源 |
| retry_scheduled | transient 只读失败，next_retry_at 已保存 | running：到期领取且尝试/时间预算未尽；failed：耗尽或永久错误；cancelled：未发写 | 错误分类、下次时间、历史尝试和用量 |
| reconciling | dispatching/outcome_unknown、写后保存失败或过期执行租约 | running：已确认归属且只进入验证；failed：拒绝/无法确认需人工；cancelled：取消且核对已闭合；不能回 queued 再 PATCH | 稳定 operation_id、UID/版本/前后快照、请求/响应/核对时间线 |
| succeeded | 图正常 END，输出已保存且无未闭合写 | 无原地重开；新请求创建关联新 run | 输出、恢复范围、完成时间；不等同业务 passed |
| failed | 永久错误/预算耗尽/业务节点失败 END/人工核对交接 | 无原地重开；显式新 run 仍需考虑旧在途操作 | errors、trace、检查点、失败分类、未闭合操作标记 |
| cancelled | 拒绝/取消已持久化，无未知在途写（有则先 reconciling） | 无原地重开；新 run 新授权 | 取消来源、时间、核对闭合依据 |

等待输入/批准后 queued 的输入模式固定为 resume，不重新用原请求从 START 执行。
审批节点自行校验；排队不是批准。终态不复用旧审批。运行中图业务失败 END 不属于
可从 pending tasks 恢复的崩溃，禁止用任意 goto 绕过校验。

## 恢复分类（2A/2B 实现）

| 现场 | 恢复行为 | 必须保留的证据 |
| --- | --- | --- |
| 无 checkpoint、无外部操作 | 只对已持久化且未执行的初始输入启动 | 输入 hash、workflow_version、领取 epoch |
| 有有效租约 | 不抢占；只返回已有状态 | DB 时间、拥有者、到期时间 |
| 只读 pending task，租约已过期 | 兼容 workflow 版本后有限续跑 | checkpoint/tasks、旧/新 epoch、尝试计数 |
| 实际 interrupt | 保持等待；仅保存了匹配输入时 Command(resume=...) | interrupt ID、question/approval 版本、输入摘要 |
| 已正常 END / 已保存业务失败 | 修复 DB 投影或保持终态，不自动重跑 | checkpoint 与领域输出、原错误 |
| checkpoint 缺失/损坏/版本不支持 | 停止并人工交接，不猜节点 | 原始检查点标识、反序列化错误类型、版本 |
| prepared 且能证明从未 dispatch | 有效拥有者重新核对授权/目标，使用同一 operation_id | 准备事务、无 dispatch 证据、当前 UID/版本 |
| dispatching/超时/写后未保存 | reconciling，先读现场，不补写 | 请求开始时间、UID/resourceVersion、响应（如有）与当前配置 |
| 当前是目标值且响应可确切绑定本操作 | 只进入验证，不再 PATCH | 实际响应 UID/版本/配置、核对结果 |
| 仍是旧值、第三种值、换 UID、或目标值但归属不明 | manual_required；现场旧值不能排除仍在途 | 全部可得快照、未知项和交接责任 |

operation 状态：prepared（写前事务）→ dispatching（请求前持久化）→ succeeded/
rejected/outcome_unknown；outcome_unknown/dispatching → reconciled/manual_required。
reconciled 只说明核对结论，另记 attribution=confirmed/not_established；不暗示因果。
prepared 的授权/UID/版本失效可 rejected；manual_required 不自动补写。
operation_id 由 run+plan_revision+approval_id+动作序号稳定绑定并 UNIQUE，不能每次
节点执行都随机生成。DB epoch 不能充当 Kubernetes fencing token，不承诺 exactly-once。

1B 初值：heartbeat=5s、lease=30s、最多 3 次领取尝试，重试退避 5s/15s，
只读依赖允许有限重试；同步 LLM 不阻塞心跳。DB 时间决定租约，CAS 控制结果提交。
写请求前再次检查拥有权与 UID/resourceVersion；已经发出的请求无法靠取消强行收回。
应用级与 SDK 重试分别记录，语义校验重试、失败调用全部计入预算。

## 验收边界

本 ADR 用于 1A/1B 直接实现，不是后台能力已完成的声明。运行层新增字段不能削弱
现有 selector/readiness 白名单、程序校验、显式审批或执行前复核。
1A 的输入/输出、失败码、迁移和并发要求已明确；后续每块实现对应部分再验收。
