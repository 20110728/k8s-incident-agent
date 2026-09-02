# Readiness Probe端口错误

## 故障类型

readiness_probe_wrong_port

## 典型症状

Pod处于Running状态，但Ready为False。Events出现Readiness probe failed，并包含connection refused、dial tcp或无法连接指定端口。Service存在，但EndpointSlice中没有可接收流量的Ready端点。

## 必查证据

- PodStatus：Pod phase与Ready状态
- PodEvents：Readiness probe failed及连接目标端口
- Deployment：readinessProbe.port、containerPort和应用启动参数
- EndpointSlice：端点Ready状态
- PodLogs：应用实际监听地址和端口

## 判断规则

同时满足以下条件时，可判断为Readiness Probe端口错误：

1. Pod phase为Running。
2. Pod Ready为False。
3. Events包含Readiness probe failed和连接拒绝或超时。
4. Deployment中的readinessProbe端口与应用实际监听端口不一致。
5. 应用已成功启动，且没有HTTP路径错误证据。

## 推荐操作

核对应用实际监听端口，将readinessProbe中的数值端口或命名端口修改为正确值。修改后等待Deployment滚动更新，并检查Pod Ready和EndpointSlice。

## 风险说明

修改探针端口时不要混淆Service port、targetPort和容器实际监听端口。删除Readiness Probe会使尚未就绪的Pod接收流量。

## 回滚方案

恢复修改前的Deployment readinessProbe配置，并回滚到上一稳定版本后重新观察Ready状态。

## 参考来源

https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
