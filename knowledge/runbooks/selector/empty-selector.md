# Service Selector为空

## 故障类型

empty_service_selector

## 典型症状

Service对象存在且spec.selector缺失或为空，但没有人为创建对应EndpointSlice。即使同一Namespace中存在Running且Ready的后端Pod，Kubernetes也不会自动为该Service选择Pod或创建EndpointSlice，访问Service时没有后端端点。

## 必查证据

- Service：spec.selector是否缺失或为空
- EndpointSlice：是否存在带kubernetes.io/service-name标签的对象
- NamespacePods：预期后端Pod是否存在且Ready
- Deployment：Pod模板标签
- Service配置：是否有意使用无selector模式

## 判断规则

同时满足以下条件时，可判断为Service Selector为空：

1. Service不是ExternalName类型。
2. Service的spec.selector缺失或为空。
3. 没有手动创建并关联到该Service的EndpointSlice。
4. Service因此没有可用后端端点。
5. 该Service原本预期通过标签自动选择集群内Pod。

## 推荐操作

如果Service应指向集群内Pod，为spec.selector配置能够准确匹配目标Pod的标签。如果无selector是有意设计，则手动创建并维护带kubernetes.io/service-name标签的EndpointSlice。

## 风险说明

无selector Service是Kubernetes支持的合法模式，不能仅凭selector为空就认定配置错误。添加selector前必须确认它不是用于外部后端或自定义EndpointSlice。

## 回滚方案

恢复Service原配置；若原本使用手动端点，则恢复先前的EndpointSlice对象及其关联标签。

## 参考来源

https://kubernetes.io/docs/concepts/services-networking/service/

https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/
