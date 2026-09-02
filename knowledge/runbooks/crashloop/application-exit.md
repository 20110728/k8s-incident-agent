# 应用进程异常退出

## 故障类型

application_exit

## 典型症状

Pod可能处于Running状态，但容器反复退出并重启。PodStatus中容器处于Waiting，reason为CrashLoopBackOff，restartCount持续增加；lastState.terminated显示非零exitCode或Error。PodLogs中通常可以看到应用异常、未捕获错误或主动退出信息。

## 必查证据

- PodStatus：容器Waiting原因、restartCount和lastState.terminated
- PodEvents：BackOff restarting failed container事件
- PodLogs：当前日志和上一次终止容器日志
- Deployment：容器command、args、env和restartPolicy相关配置
- Nodes：所在节点是否存在异常状态

## 判断规则

同时满足以下条件时，可判断为应用进程异常退出：

1. 容器restartCount持续增加。
2. 容器当前reason为CrashLoopBackOff或事件包含BackOff restarting failed container。
3. lastState.terminated显示非零exitCode或reason为Error。
4. 上一次容器日志包含应用异常、致命错误或主动退出信息。
5. 没有更明确的Invalid Command、OOMKilled或缺失配置证据。

## 推荐操作

优先读取上一次终止容器日志，定位应用异常和退出位置。修复应用代码、依赖服务或启动参数后发布新镜像，并观察滚动更新期间的restartCount和Ready状态。

## 风险说明

仅增加重启次数或延长CrashLoopBackOff等待时间不会消除应用故障。直接修改restartPolicy可能使容器停止重启，但会降低服务可用性并掩盖根因。

## 回滚方案

将Deployment回滚到最近一个稳定版本，确认旧版本Pod恢复Ready且restartCount不再增加。

## 参考来源

https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/

https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/
