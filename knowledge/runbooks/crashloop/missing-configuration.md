# 应用配置缺失

## 故障类型

missing_configuration

## 典型症状

容器因缺少环境变量、配置文件、ConfigMap或Secret而无法启动。Pod可能处于CreateContainerConfigError；如果配置由应用自行校验，容器可能启动后立即退出并进入CrashLoopBackOff。Events或PodLogs出现not found、missing required configuration、couldn't find key等信息。

## 必查证据

- PodStatus：Waiting原因、状态消息、restartCount和lastState
- PodEvents：ConfigMap、Secret、key或volume挂载失败事件
- Deployment：env、envFrom、volumes和volumeMounts中的配置引用
- PodLogs：应用是否报告必需配置缺失
- Namespace资源：被引用的ConfigMap或Secret是否存在且名称、key正确

## 判断规则

满足以下任一证据链时，可判断为应用配置缺失：

1. Pod处于CreateContainerConfigError，Events明确指出引用的ConfigMap、Secret或key不存在。
2. 容器反复重启，PodLogs明确指出必需的环境变量或配置文件缺失。
3. Deployment引用的配置对象名称、key或挂载路径与实际资源不一致。
4. 故障与缺失配置直接相关，且没有Invalid Command或OOMKilled证据。

## 推荐操作

在同一Namespace中创建或恢复缺失的ConfigMap、Secret或key，或者修正Deployment中的引用名称和挂载路径。确认配置内容正确后重新创建Pod或触发Deployment滚动更新。

## 风险说明

Secret可能包含敏感信息，不得将其值写入日志或故障报告。不要将optional设置为true来绕过应用必需配置，否则容器可能以不完整配置运行。

## 回滚方案

恢复上一版本的ConfigMap、Secret和Deployment引用；若配置变更已触发发布，则回滚到上一稳定Deployment版本。

## 参考来源

https://kubernetes.io/docs/concepts/configuration/configmap/

https://kubernetes.io/docs/concepts/configuration/secret/

https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/
