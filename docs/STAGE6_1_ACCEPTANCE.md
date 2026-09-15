# 第六阶段第一步：处置后业务恢复验证

基线为本次上传的 k8s-current-full-source.tar.gz；本包只包含该基线的增量。

## 行为

生产依赖 build_recovery_verifier 使用 BusinessRecoveryVerifier 包装既有资源验证器。
先保留资源等待结果；只有该结果 succeeded 才重新采集资源、服务配置和登记业务检查。
新证据写入 verification_result.post_repair_evidence，不替换诊断/审批 evidence。
API 已引用 RecoveryVerificationResult，因此无需重复修改 API 模型或新增数据库列。

总体 status=succeeded 必须同时满足：
- 原资源验证通过；
- 新采集仍匹配原登记配置摘要和 Deployment UID；
- generation 与本次动作一致（selector 不变；实际 readiness patch 增加一次）；
- 新资源事实 ready，无已识别漂移和当前运行故障；
- 本次全部登记业务检查 passed。

业务 failed 与 unknown 均不报告成功，分别保留 business_status；总体 failed 表示验证未通过，不自动断言应用故障。
额外发布、资源重建、登记变化等返回 POST_REPAIR_OBSERVATION_INVALID，需要重新调查。
检查器及其既有结果绑定、超时、响应限制保持不变；业务检查只执行一批，不新增 LLM 调用或写操作。
原资源验证默认等待 60 秒；其后完整只读采集会增加耗时，60 秒不是整条验证的总时限。

resource_verification_status 是先前资源等待结果，resource_status 是新采集时的资源事实，两者可能不同。
旧检查点默认 verification_scope=resource_only / business_status=skipped，页面明确标注未验证业务。
本次 passed 仅限集群内登记 Service 接口及采样窗口，不覆盖外部入口、所有接口、所有副本或持续可用性。

## 安装

将交付包上传 ECS 的 ~/ 后，在项目根目录执行：

```bash
mkdir -p /tmp/k8s-stage6-1
tar -xzf ~/k8s-incident-agent-v02-stage6-1.tar.gz -C /tmp/k8s-stage6-1
git apply --check /tmp/k8s-stage6-1/k8s-incident-agent-v02-stage6-1/stage6-1.patch
git apply /tmp/k8s-stage6-1/k8s-incident-agent-v02-stage6-1/stage6-1.patch
python -m pytest backend/tests/business_recovery -q
```

预期 20 passed。补丁 check 失败时停止，不使用覆盖或强制应用。

相关回归：

```bash
python -m pytest backend/tests/business_recovery backend/tests/agent/test_verification.py backend/tests/agent/test_executor_and_node.py backend/tests/api backend/tests/diagnosis_policy -q
npm --prefix frontend run build
npm --prefix frontend test
```

后端预期 149 passed；前端构建成功、35 tests passed。前端无依赖目录时先执行 npm --prefix frontend ci。
本地未启动真实 Kubernetes/PostgreSQL；序列化/API 测试不等于真实重启恢复验收。

## 真实验收

先运行已有正常恢复脚本，再更新当前运行实例。使用 Compose 时：

```bash
bash scripts/stage5_fault.sh reset
bash scripts/compose_up.sh
```

使用 uvicorn/Vite 开发模式时，在原启动终端重启对应服务即可，不同时启第二套监听。

逐个验收 selector 和 readiness，每次都新建事件，不能复用旧事件：

```bash
bash scripts/stage5_fault.sh selector_mismatch
```

网页提交 agent-demo / order-service，描述“检查当前发布配置与业务状态”。
确认计划是 patch_service_selector 且目标和值符合登记配置后人工批准。
若没有生成允许计划，停止并反馈 diagnosis/remediation_plan；不要绕过校验。
流程完成后复制事件 ID：

```bash
python -m scripts.check_post_repair --incident-id 实际事件ID --expect-business passed
```

预期 phase=verification_succeeded，verification_scope=resources_and_registered_business，
resource_verification_status=succeeded，resource_status=ready，business_status=passed，passed=true。
post_repair_evidence 中有新的 BusinessCheck，且 unverified_scope 非空。

然后准备 readiness：

```bash
bash scripts/stage5_fault.sh readiness_path_error
```

新建事件，确认 patch_readiness_probe 计划后批准，再对新事件运行相同查询命令。
预期同上。探针修复前旧副本可能仍提供业务；不能用旧业务通过记录代替本次新检查。

最后恢复：

```bash
bash scripts/stage5_fault.sh reset
python -m scripts.run_fault_case --case normal --source live --mode facts
```

预期 passed=true。
若使用 Compose，可再次运行 bash scripts/compose_up.sh 重建后端实例（保留数据库），
然后查询已完成事件，确认新增结果仍在。查询本身不重新运行验证。

此次验证失败场景由确定性测试覆盖，不能据此宣称真实集群故障反例已验收。
20 个新增测试不调用真实模型；上述网页新建事件可能调用原有诊断/规划模型。

## 已有基线问题

扩大回归发现 backend/tests/agent/test_execution_policy.py 中 4 项失败：
- test_approved_plan_is_authorized
- test_completed_execution_is_not_repeated[succeeded]
- test_completed_execution_is_not_repeated[already_applied]
- test_failed_execution_requires_new_approval

同一环境在未修改的上传源码上复现 4 failed / 6 passed；不是本轮引入。
这些旧状态夹具无法通过当前配置与诊断依据门槛。本轮未修改这些测试或放宽执行规则。
本轮不宣称全仓测试通过。

## 下一步

本次仅为第六阶段第一步，不标记第六阶段整体验收完成。
人工处理后只读复查入口留到反馈通过后继续；不改自主调查框架、审批角色或写工具范围。
