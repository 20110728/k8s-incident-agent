# 阶段 2：人工处理后的只读复查后端

## 行为

- POST `/api/v1/incidents/{incident_id}/rechecks`，请求体 `{"note":"处理说明"}`。
  只读取 Kubernetes，采集结果独立持久化，不调用模型、不修改集群、不恢复 LangGraph。
- GET 同一路径，读取历史，不重新采集。默认每页 20 条，上限 50 条，
  通过返回的 next_before_sequence 作为下一页 before_sequence 查询。
- 新增数据库迁移 2 和 `incident_agent_app.rechecks` 表；后端启动时自动执行迁移。
  独立记录不会覆盖旧诊断、审批、执行与 Recovery verification。
- 支持人工调查结束、诊断/规划失败、拒绝审批、执行失败/冲突、恢复验证结束等终态。
  等待审批或执行中的事件拒绝复查（409）；无此事件返回 404。
- note 仅作为未验证的用户说明保存，不作为判断依据，也不冒充已认证的操作者身份。

## 判定与目标变化

资源 ready、登记业务 passed、登记探针匹配且无当前配置/运行故障，才返回 status=passed。
存在明确故障返回 failed，证据不足或采集异常返回 unknown。未知不借用历史成功。
采用阶段 1 的退出 Pod 判定视图，原始证据保留；不增加延时或连续业务采样。

与原事件比较 profile digest、Deployment UID、generation。人工发布或同名重建后，
只要新的 profile 正确登记、现场证据匹配，可以返回 passed + target_comparison.status=changed。
未登记的新版本不能算通过。same_observed_fields 仅指这几个字段未变，
不代表 Service/Pod 完整身份或所有配置均未改变。
recovery_attribution 永远为 not_established，不把人工处理后的健康状态归功于 Agent。

## 重试、并发和异常

每次 POST 是独立的新观察，重复 POST 会创建不同记录；客户端不要自动重试 POST。
并发复查以独立记录保存，按数据库 sequence 降序分页；不是基于同一数组读改写。
sequence 表示插入顺序，采样时间另有 started_at/finished_at。
进程在保存前退出的请求可能没有记录，不能声称观察已保存；请求失败后先 GET 查询历史。
数据库保存失败返回 503，不返回“已保存成功”。本阶段不新增分布式执行锁。

## 验收

1. 定向测试：`python -m pytest -q backend/tests/api/test_rechecks.py backend/tests/persistence/test_migrations.py backend/tests/api/test_incidents_api.py backend/tests/api/test_lifespan.py backend/tests/api/test_dependencies.py`
2. 重启 backend 自动迁移；profile、检查器、前端不变。
3. 对原人工调查事件执行 `python -m scripts.recheck_incident --incident-id ID --note '人工处理后复查' --expect passed`。
4. 记录返回的 recheck_id，重启 backend，使用 `--history --expect-recheck-id RECHECK_ID` 验证持久化。
5. 可再次 POST 并使用 history 确认两条不同记录；original_incident_unchanged 应为 true。

当前页面不新增按钮，完整交互在阶段 3 实施；GET 原事件不会自动附带复查历史，
调用方通过新端点读取。原事件 phase 保持原值是正常行为。

本地定向测试 50 项通过，数据库迁移与仓储调用采用 fake/Mock 验证；
真实 PostgreSQL 重启持久化、实际 Kubernetes 采集由 ECS 验收确认。

## 阶段存档验收反馈（2026-09-15）

维护者反馈：原人工调查事件的只读复查通过，重启 backend 后复查记录仍存在。
这是运行环境的验收反馈，本次文档整理没有重新执行集群请求。页面入口仍留待阶段 3。
