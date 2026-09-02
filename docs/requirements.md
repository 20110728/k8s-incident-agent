# 项目目标

用户输入 Kubernetes Service 和故障描述后，系统采集集群证据，
判断故障根因，生成处理建议，并在人工批准后执行白名单操作。

# MVP支持范围

命名空间：agent-demo
入口资源：Service
定位链路：Service → Pod → ReplicaSet → Deployment → Node

支持诊断：
1. CrashLoopBackOff
2. ImagePullBackOff
3. OOMKilled
4. Readiness Probe错误
5. Service Selector错误

允许写操作：
1. patch_readiness_probe
2. patch_service_selector
3. restart_deployment（可选）

# 第一周范围

| 日期    | 调整后的内容                                          |
| ----- | ----------------------------------------------- |
| 第1天上午 | 购买Ubuntu云服务器、配置SSH和安全组                          |
| 第1天下午 | 安装Docker、kubectl、kind、Python和VS Code Remote SSH |
| 第2天   | 创建远程kind集群和正常Demo服务                             |
| 第3天   | 制作并验证5类故障YAML                                   |
| 第4～5天 | 编写Kubernetes Python只读工具                         |
| 第6天   | RBAC、Mock单元测试和权限验证                              |
| 第7天   | 完成不使用LLM的规则诊断脚本                                 |
