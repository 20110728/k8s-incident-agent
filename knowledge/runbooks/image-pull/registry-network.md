# 镜像仓库网络异常

## 故障类型

registry_network_error

## 典型症状

Pod处于Pending，容器状态为ErrImagePull或ImagePullBackOff。Events中的拉取失败消息包含i/o timeout、context deadline exceeded、TLS handshake timeout、connection refused、no such host或临时DNS解析失败。多个使用同一Registry的Pod可能同时受影响。

## 必查证据

- PodStatus：容器Waiting原因和状态消息
- PodEvents：Failed to pull image中的网络错误详情
- Deployment：镜像Registry地址和端口
- Nodes：故障Pod所在节点及节点状态
- 对比证据：相同镜像在其他节点是否能够拉取

## 判断规则

同时满足以下条件时，可判断为镜像仓库网络异常：

1. 容器reason为ErrImagePull或ImagePullBackOff。
2. Events包含超时、连接拒绝、DNS解析失败或TLS连接失败信息。
3. 镜像引用格式有效，且没有not found或unauthorized证据。
4. Registry服务、节点DNS、代理、防火墙、路由或证书链中至少一项异常。
5. 若仅特定节点失败，相同镜像在其他节点可正常拉取。

## 推荐操作

从故障节点检查Registry域名解析、TCP/TLS连通性、代理和防火墙配置，并确认Registry服务可用。修复网络或证书链后，让kubelet继续重试或重新创建Pod。

## 风险说明

不要通过关闭TLS校验或将Registry设为不安全仓库来临时绕过证书问题，这会降低镜像供应链安全性。修改节点级代理和DNS可能影响该节点上的全部工作负载。

## 回滚方案

恢复修改前的节点DNS、代理、防火墙或Registry证书配置；必要时将工作负载临时回滚到节点已有缓存的稳定镜像。

## 参考来源

https://kubernetes.io/docs/concepts/containers/images/

https://kubernetes.io/docs/tasks/debug/debug-cluster/
