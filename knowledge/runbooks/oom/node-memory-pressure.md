# 节点内存压力

## 故障类型

node_memory_pressure

## 典型症状

节点Condition中MemoryPressure为True，多个Pod可能同时异常。Pod可能被驱逐并显示Failed或Evicted，Events和状态消息包含The node was low on resource: memory。受影响容器不一定出现OOMKilled。

## 必查证据

- Nodes：MemoryPressure Condition、allocatable和节点事件
- PodStatus：phase、reason和message是否为Evicted
- PodEvents：Evicted、memory pressure或节点资源不足事件
- Deployment：resources.requests.memory和resources.limits.memory
- NamespacePods：同一节点上是否有多个Pod同时异常

## 判断规则

同时满足以下条件时，可判断为节点内存压力：

1. Pod所在节点的MemoryPressure为True，或节点内存达到驱逐阈值。
2. PodStatus或Events包含Evicted、memory pressure或节点内存不足信息。
3. 同一节点上的一个或多个Pod受到影响。
4. 故障原因是节点级可用内存不足，而不是单个容器明确超过自身memory limit。

## 推荐操作

定位节点上的主要内存消费者，修正不合理的requests和limits，并通过扩容、迁移负载或安全驱逐释放容量。处理完成后确认MemoryPressure恢复为False，再恢复工作负载。

## 风险说明

直接删除Pod可能使其重新调度回同一高压节点。盲目提高容器limit会进一步加剧节点压力；驱逐或排空节点前必须评估副本数和业务可用性。

## 回滚方案

若迁移或资源调整导致容量不足，恢复原调度配置并逐步撤销变更；对节点执行操作时按原计划解除cordon并确认关键Pod恢复Ready。

## 参考来源

https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/

https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
