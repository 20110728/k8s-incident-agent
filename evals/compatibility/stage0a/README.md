# 0A 旧契约样本

基线：`67f310d184eb91c6c07a21752c832543eb3301ae`。

三份文件均为 **synthetic / 合成样本**，不来自 ECS，不是历史实测证据，
不可以导入生产数据库或当作已批准执行请求。

- `awaiting_approval.json`：基于旧 `v02-stage5/changed_after_approval.json`
  的 state、stub_diagnosis、approval_plan，固定事件身份，以基线
  `build_approval_request()` 生成绑定，保留 profile/诊断/证据。
- `terminal.json`：按旧 `RecoveryVerificationResult` 必填字段构造，刻意
  缺少新版业务验证字段，要求读取后仍为 resource_only / skipped。
- `recheck.json`：按基线 RecheckResult 构造的独立 unknown 观察，关联终态样本；
  不冒充有实际采集依据的 passed 记录。

时间及 ID 均为固定测试值。未包含凭据、真实日志、真实操作者或 ECS 地址。
文件冻结后不随模型或校验器自动重新生成；兼容失败应调查，不能重写样本消除失败。

读取校验：

```bash
python -m unittest backend.tests.compatibility.test_stage0a_samples -v
```

这只验证结构读取、默认值、绑定与反例，不证明 PostgresSaver 二进制序列化兼容、
旧检查点可续跑、实际审批权限或真实数据库持久化。真实三类样本仍须按
`docs/acceptance/0A-baseline.md` 在 ECS 私有目录留存并验证；未经脱敏不得提交。
