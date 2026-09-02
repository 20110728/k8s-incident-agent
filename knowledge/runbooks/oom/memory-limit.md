# 容器内存限制过低

## 故障类型

memory_limit_exceeded

## 典型症状

容器运行一段时间后被终止并重启，PodStatus中lastState.terminated.reason为OOMKilled，常见exitCode为137。Deployment为容器设置了memory limit，restartCount持续增加；节点本身不一定处于MemoryPressure。

## 必查证据

- PodStatus：lastState.terminated.reason、exitCode和restartCount
- PodEvents：BackOff、OOM或容器重启相关事件
- Deployment：resources.requests.memory和resources.limits.memory
- Nodes：MemoryPressure状态和可分配内存
- PodLogs：被终止前的内存分配、并发量或负载信息

## 判断规则

同时满足以下条件时，可判断为容器内存限制过低：

1. lastState.terminated.reason为OOMKilled。
2. 容器配置了明确的memory limit。
3. 故障在内存使用接近或超过该limit时发生。
4. 所在节点MemoryPressure不为True，Pod也没有Evicted证据。
5. 应用在合理工作负载下的正常内存需求高于当前limit，且暂无线性持续增长的泄漏证据。

## 推荐操作

根据稳定负载下的实际内存峰值和安全余量调整memory request与limit，同时检查缓存、并发和内存型emptyDir的使用。更新后观察OOMKilled、restartCount和节点容量。

## 风险说明

直接大幅提高limit可能把压力转移到节点并导致其他Pod被驱逐。memory request过低也可能造成调度过密；调整前应确认节点容量和业务峰值。

## 回滚方案

恢复修改前的resources配置；若新配置引发节点压力，则立即回滚并临时降低并发或副本负载。

## 参考来源

https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/
