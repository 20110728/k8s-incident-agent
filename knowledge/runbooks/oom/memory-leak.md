# 应用内存泄漏

## 故障类型

memory_leak

## 典型症状

容器在相似负载下运行一段时间后内存持续增长，最终被OOMKilled并重启；重启后内存回落，随后重复相同增长过程。PodStatus中restartCount周期性增加，PodLogs可能在终止前出现内存分配失败或垃圾回收压力。

## 必查证据

- PodStatus：多次OOMKilled记录和restartCount变化
- PodEvents：容器重启与BackOff事件
- Deployment：memory request、limit和应用版本
- 内存时序指标：容器工作集随时间的变化趋势
- PodLogs：异常分配、缓存膨胀或资源未释放线索
- Nodes：排除MemoryPressure导致的驱逐

## 判断规则

同时满足以下条件时，可判断为应用内存泄漏：

1. 容器发生一次以上OOMKilled，重启后暂时恢复。
2. 在相似负载下，内存使用随运行时间持续增长且不回落。
3. 多个重启周期呈现相似的“增长—OOMKilled—回落”模式。
4. 节点MemoryPressure不为True，且提高limit只会延后而不会消除OOM。
5. 已排除一次性峰值、合理缓存增长和单纯limit过低。

## 推荐操作

保留内存时序指标和终止前日志，使用应用运行时的heap profile或内存分析工具定位未释放对象、无界缓存和连接资源。修复代码后进行持续负载验证，再发布新版本。

## 风险说明

仅凭一次OOMKilled不能判定内存泄漏。单纯提高memory limit通常只会延迟故障，并可能扩大节点内存压力和故障影响范围。

## 回滚方案

回滚到无持续内存增长的稳定应用版本，并恢复经验证的resources配置；在修复前可临时限制并发或缓存规模。

## 参考来源

https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/
