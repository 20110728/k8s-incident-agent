# Service Selector与Pod标签不匹配

## 故障类型

selector_label_mismatch

## 典型症状

Service对象存在，但没有自动生成可用端点，或其EndpointSlice中endpoints为空。后端Pod处于Running且Ready为True，但Service selector不能匹配这些Pod的labels，访问Service超时或连接失败。

## 必查证据

- Service：spec.selector
- PodStatus：后端Pod的Ready状态
- NamespacePods：候选Pod的metadata.labels
- EndpointSlice：是否存在以及endpoints数量
- Deployment：spec.template.metadata.labels

## 判断规则

同时满足以下条件时，可判断为Service Selector与Pod标签不匹配：

1. Service配置了非空spec.selector。
2. 候选Pod处于Running且Ready为True。
3. 没有任何候选Pod的labels同时满足Service selector的全部条件。
4. Service对应的EndpointSlice不存在或endpoints为空。
5. Service和Pod位于同一Namespace。

## 推荐操作

明确Service应指向的工作负载，统一修改Service selector或Deployment Pod模板标签，使键和值完全匹配。修改后确认EndpointSlice自动出现Ready端点。

## 风险说明

放宽selector可能把流量发送到不属于该服务的Pod。修改Deployment selector风险更高且apps/v1中不可变，通常优先修改Service selector或Pod模板中的非控制器标签。

## 回滚方案

恢复原Service selector和Deployment Pod模板标签，并重新确认原有流量路径和EndpointSlice。

## 参考来源

https://kubernetes.io/docs/concepts/services-networking/service/

https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/
