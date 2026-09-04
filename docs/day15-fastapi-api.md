# Day 15：FastAPI 基础、API 契约和应用服务层

## 完成内容

Day 15 在已有 LangGraph 工作流之外增加了 HTTP API 边界，
没有修改 Day 1～14 的诊断、审批、执行或恢复验证逻辑。

已完成：

- FastAPI 应用工厂和应用入口；
- 非敏感 API 配置；
- health 和 readiness 接口；
- 创建事故请求模型；
- 事故状态响应模型；
- Graph 应用服务封装；
- 惰性依赖构造和进程内单例；
- 创建事故接口；
- 事故状态查询接口；
- 统一错误响应；
- OpenAPI 接口定义；
- Fake Graph、Fake Service 和分层集成测试。

## 应用入口

FastAPI 应用位于：

```text
backend.app.main:app