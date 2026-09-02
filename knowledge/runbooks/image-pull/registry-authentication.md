# 镜像仓库认证失败

## 故障类型

registry_authentication_error

## 典型症状

Pod处于Pending，容器状态为ErrImagePull或ImagePullBackOff。Events中的拉取失败消息包含unauthorized、authentication required、pull access denied、failed to authorize或no basic auth credentials。

## 必查证据

- PodStatus：容器Waiting原因和状态消息
- PodEvents：镜像拉取认证失败详情
- Deployment：image和imagePullSecrets配置
- ServiceAccount：是否配置默认imagePullSecrets
- Namespace资源：引用的docker-registry类型Secret是否存在

## 判断规则

同时满足以下条件时，可判断为镜像仓库认证失败：

1. 容器reason为ErrImagePull或ImagePullBackOff。
2. Events包含unauthorized、authentication required、pull access denied或failed to authorize。
3. 目标镜像位于需要认证的私有Registry，或匿名拉取不被允许。
4. imagePullSecrets缺失、Secret不存在、Secret类型或Registry地址不匹配，或者凭据已失效。
5. 没有manifest unknown或网络超时证据。

## 推荐操作

在Pod所在Namespace中创建或更新有效的kubernetes.io/dockerconfigjson Secret，并在Deployment或其ServiceAccount中正确引用imagePullSecrets。重新创建Pod后确认镜像能够拉取。

## 风险说明

不得在Deployment、日志或故障报告中明文保存Registry密码或token。更新共享ServiceAccount上的imagePullSecrets可能影响同一Namespace内的其他工作负载。

## 回滚方案

恢复上一版本仍有效的镜像拉取Secret及其引用；如镜像同时发生变更，则回滚到上一可拉取的镜像版本。

## 参考来源

https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/

https://kubernetes.io/docs/concepts/containers/images/
