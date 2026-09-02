# 容器镜像不存在

## 故障类型

image_not_found

## 典型症状

Pod长时间处于Pending，容器状态为ErrImagePull或ImagePullBackOff。Events出现Failed to pull image，并包含manifest unknown、not found、repository does not exist或找不到指定tag等信息。

## 必查证据

- PodStatus：容器Waiting原因和状态消息
- PodEvents：Failed、ErrImagePull和BackOff pulling image事件
- Deployment：完整image名称、registry、repository和tag或digest
- OwnerChain：确认故障Pod使用的Deployment版本
- Nodes：排除仅单个节点发生的基础设施异常

## 判断规则

同时满足以下条件时，可判断为容器镜像不存在：

1. Pod phase为Pending或容器尚未启动。
2. 容器reason为ErrImagePull或ImagePullBackOff。
3. Events包含manifest unknown、not found或repository does not exist等信息。
4. Deployment配置的镜像仓库、名称、tag或digest在目标Registry中不存在。
5. 没有unauthorized、timeout或DNS失败等认证或网络证据。

## 推荐操作

核对Deployment中的完整镜像引用，确认仓库名称、tag或digest已经发布。修正为存在且经过验证的镜像版本后触发滚动更新。

## 风险说明

不要仅将tag改为latest来规避问题；latest可能指向不可预测的镜像，并受imagePullPolicy影响。生产环境优先使用固定tag或digest。

## 回滚方案

将Deployment镜像回滚到最近一个可成功拉取并正常运行的tag或digest。

## 参考来源

https://kubernetes.io/docs/concepts/containers/images/

https://kubernetes.io/docs/tutorials/kubernetes-basics/update/update-intro/
