# Deployment Selector与Pod模板标签不匹配

## 故障类型

deployment_label_mismatch

## 典型症状

创建或更新Deployment时，API Server拒绝请求并提示selector does not match template labels。新的Deployment不会成功创建；更新已有Deployment时，旧配置继续生效。apps/v1中的Deployment spec.selector在创建后不可变。

## 必查证据

- 部署操作结果：kubectl apply或API Server返回的校验错误
- Deployment清单：spec.selector.matchLabels或matchExpressions
- Pod模板：spec.template.metadata.labels
- 现有Deployment：当前生效的selector和generation
- ReplicaSet：是否仍由旧版本Deployment管理Pod

## 判断规则

同时满足以下条件时，可判断为Deployment Selector与Pod模板标签不匹配：

1. 提交的是apps/v1 Deployment。
2. spec.selector不能匹配spec.template.metadata.labels。
3. API Server返回selector does not match template labels或等价校验错误。
4. 新Deployment未创建，或已有Deployment仍保持修改前的generation和配置。

## 推荐操作

在应用清单前统一Deployment spec.selector和spec.template.metadata.labels。若只需增加业务标签，不要修改已有不可变selector；若必须更换selector，应评估后创建新的Deployment并迁移流量。

## 风险说明

Deployment selector在apps/v1中不可变。删除并重建Deployment可能造成短暂中断、孤立ReplicaSet或错误接管其他Pod；重叠selector还会导致控制器相互干扰。

## 回滚方案

撤销未生效的清单修改并继续使用当前Deployment。若已通过新Deployment迁移，则将Service流量切回旧Deployment并删除确认无用的新资源。

## 参考来源

https://kubernetes.io/docs/concepts/workloads/controllers/deployment/

https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/
