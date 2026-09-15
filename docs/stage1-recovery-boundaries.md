# 阶段 1：恢复判定边界

基线：用户提供的 `k8s-incident-agent-current-89b5367.tar.gz`。
本阶段不增加写动作、人工复查入口、LLM 调查或延时稳定性验证。

## 当前恢复时序

1. `KubernetesRecoveryVerifier` 立即读取现场；默认资源等待窗口为 60 秒。
2. 未通过时等待 2 秒，再读取现场；任一轮全部检查通过即返回。
3. Kubernetes API 异常直接返回失败，不走上述条件未满足的轮询。
4. `BusinessRecoveryVerifier` 仅在资源等待成功后，重新采集一次资源和登记业务检查。
5. 新资源状态与登记业务结果共同决定最终结果；未知不等于成功。

`attempts` 是第一部分的资源观察次数，不是模型重试次数，也不是业务连续通过次数。
每次资源读取有独立请求耗时，60 秒不是端到端硬截止；当前也不是固定 30 轮。
现有业务检查采集有自己的时间预算，不能与资源等待窗口相加后宣称为严格总时限。
没有成功后再等数秒的第二轮验证，也没有连续 N 次业务成功的要求。
持续可用性仍在 `unverified_scope` 中。

## 本次修改

- Service profile 增加可选的 `expected_replicas`，Demo revision 从 2 升到 3，登记 2 个副本。
  现场 desired_replicas 必须与登记值相同，现有 Ready/available/Pod 数量检查仍保留。
  新字段纳入 profile digest；未登记该字段的历史 profile 保持原 digest，不改写历史事件。
- `readiness_configuration_status` 为 `matched` / `drift` / `unknown`。
  path、port、scheme 均符合登记才算 matched；明确采集到没有 HTTP probe 算 drift；
  字段缺失或不完整算 unknown。采集器以 null 表示没有 HTTP probe，也可能是其他类型的探针，
  因此不据此断言完全没有任何探针。
- `readiness_patch_supported` 只表示具备当前 path/port 写工具的候选条件，
  不等于获得执行授权；后续诊断、计划、审批、执行前校验全部保留。
- 恢复结果追加 `registered_replica_count` 与 `registered_readiness_configuration` 检查，
  当前页面已有通用 checks 展示，不需要构建前端。
- retiring Pod 排除仍须匹配 Service EndpointSlice、Pod 名称/IP，且所有相关端点满足
  terminating=true、ready=false、serving=false。额外要求已匹配 profile、所属工作负载、
  正确 namespace、唯一且未就绪的 PodStatus；不修改原始证据。
- 测试 fixture 从一个副本补齐为真实的两个副本模型；原证据序号保持不变，
  两个“移除业务证据”测试改为按业务证据移除，不再假定它是最后一个元素。

## 验收范围

新增 45 项边界测试；结合受影响的诊断、恢复、业务检查、固定案例、资源轮询和调试测试，
本地定向测试共 183 项通过。测试使用独立 Python 3.12 环境，未调用真实模型、数据库或 Kubernetes。
10 个固定案例仍为原登记的合成回放；没有修改 fixture 预期答案来使结果通过。

重点反例：仍在 serving、条件未知、IP 错误、跨 Service/namespace、互相矛盾的端点、
缺少替换副本、现场缩容、探针 scheme 错误、HTTP probe 缺失、业务 unknown。

## ECS 应用后的必要操作

profile 摘要改变后，集群内业务检查器登记 targets 也必须更新。只同步 ConfigMap 并重启
检查器，无需重建检查器镜像；后端源码挂载生效需 restart backend，无需重建前端。
请使用新事件验收。旧审批与旧 profile 摘要绑定，不应在本阶段部署后继续尝试执行。

验收顺序：补丁基线核对 → 定向测试 → 同步检查器 → 重启后端 → 正常 facts →
一次新的 readiness 审批修复及恢复结果。此次共享判定有变化，所以这一次真实恢复有必要。
不重新跑全部历史真实模型案例。

通过标准：新 profile revision=3、expected_replicas=2；正常 facts 为 ready/passed；
新 readiness 事件通过审批后达到 verification_succeeded；新增两项 check 均通过；
post_repair_evidence 保存；未验证范围保留。出现模型错误应保留 llm_debug，
不能因本地边界测试通过而宣称模型稳定。

## 本阶段仍未覆盖

- Pod/EndpointSlice UID 与 deletion timestamp 的完整身份关联。
- 写后实际 UID/generation 绑定，仍沿用原恢复归因逻辑，后续阶段实施。
- 多轮业务采样、成功后延时复查、端到端硬时限。
- 所有副本逐一业务验证、外部入口及持续可用性。
