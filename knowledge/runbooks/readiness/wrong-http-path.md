# Readiness Probe HTTP路径错误

## 故障类型

readiness_probe_error

## 典型症状

Pod处于Running状态，但Ready为False。Service存在，但EndpointSlice中没有可接收流量的Ready端点。Pod事件出现Readiness probe failed或HTTP状态码404。

## 必查证据

- PodStatus：Pod phase与Ready状态
- PodEvents：Readiness probe failed事件
- Deployment：readinessProbe.path和port
- EndpointSlice：端点Ready状态
- PodLogs：应用是否正常启动

## 判断规则

同时满足以下条件时，可判断为Readiness Probe HTTP路径错误：

1. Pod phase为Running。
2. Pod Ready为False。
3. Events包含Readiness probe failed。
4. HTTP探针返回404。
5. Deployment配置中的探针路径不是应用实际健康检查路径。

## 推荐操作

核对应用实际健康检查接口，将Deployment中readinessProbe.httpGet.path修改为正确路径。修改后等待Deployment滚动更新。

## 风险说明

修改错误的探针路径可能继续导致Pod无法接收流量。不得删除Readiness Probe来掩盖应用未就绪问题。

## 回滚方案

恢复修改前的Deployment readinessProbe配置，并重新观察Pod Ready状态和EndpointSlice。

## 参考来源

https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/