# 应用启动过慢

## 故障类型

slow_startup

## 典型症状

Pod处于Running但在启动阶段长时间Ready为False，Events反复出现Readiness probe failed。应用日志显示仍在加载数据、迁移数据库或等待依赖；经过一段时间后探针可能成功。如果Liveness Probe也过早执行，容器可能在完成启动前被反复重启。

## 必查证据

- PodStatus：Ready状态、容器启动时间和restartCount
- PodEvents：Readiness、Startup或Liveness probe failed事件及时间顺序
- Deployment：startupProbe、readinessProbe和livenessProbe的时序参数
- PodLogs：应用初始化开始和完成时间
- EndpointSlice：端点何时转为Ready

## 判断规则

同时满足以下条件时，可判断为应用启动过慢：

1. 容器进程能够启动，Pod phase为Running。
2. 启动阶段Readiness Probe连续失败，但日志显示应用仍在正常初始化。
3. 实际初始化时间超过initialDelaySeconds或failureThreshold与periodSeconds提供的容忍窗口。
4. 探针路径和端口正确，应用完成初始化后探针能够成功。
5. 如果发生重启，Liveness或Startup Probe失败发生在应用初始化完成之前。

## 推荐操作

为慢启动应用配置startupProbe，并将failureThreshold乘以periodSeconds设置为能够覆盖最坏启动时间的窗口。Readiness Probe继续用于控制流量，Liveness Probe用于启动完成后的存活检测。

## 风险说明

容忍窗口设置过短会导致启动循环；设置过长则会延迟发现真正的启动失败。不要用过大的固定initialDelaySeconds替代对启动过程的准确检测。

## 回滚方案

恢复修改前的探针参数；如果新应用版本启动时间异常增长，则回滚到上一稳定版本。

## 参考来源

https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
