# 容器启动命令错误

## 故障类型

invalid_command

## 典型症状

Pod无法稳定运行，容器可能处于CreateContainerError、RunContainerError或CrashLoopBackOff。Pod事件或容器状态消息出现executable file not found、no such file or directory、exec format error等信息，restartCount可能持续增加。

## 必查证据

- PodStatus：容器Waiting原因、状态消息和lastState.terminated
- PodEvents：容器创建或启动失败事件
- Deployment：容器image、command和args
- PodLogs：应用启动前后是否产生日志
- OwnerChain：确认故障Pod对应的Deployment版本

## 判断规则

同时满足以下条件时，可判断为容器启动命令错误：

1. 容器未进入稳定Running状态，或进入后立即退出。
2. PodStatus或Events包含executable file not found、no such file or directory、exec format error等启动错误。
3. Deployment显式配置了command或args。
4. 配置的可执行文件路径、参数格式或镜像架构与镜像实际内容不一致。
5. PodLogs为空或只包含启动阶段错误，且没有OOMKilled证据。

## 推荐操作

核对镜像默认ENTRYPOINT和CMD，以及Deployment中的command和args。将可执行文件路径、解释器、参数或镜像架构修正为镜像实际支持的值，再触发滚动更新。

## 风险说明

Kubernetes中的command和args会覆盖镜像默认启动配置。盲目删除或替换它们可能导致容器执行错误入口、跳过初始化流程或以错误参数启动。

## 回滚方案

恢复修改前可正常运行的image、command和args配置，并回滚到上一稳定ReplicaSet。

## 参考来源

https://kubernetes.io/docs/tasks/inject-data-application/define-command-argument-container/

https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
