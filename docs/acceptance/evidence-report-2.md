# 块 2：模型输入表示

起始基线：`1fe6810`，实现提交 `02d0c1f`。2026-10-12 后续真实 dependency 导出已确认 v3.2 表示生效、异常原文保留；最新完整动态回归仍待反馈，不能据此声称全场景 Token 降幅。本块未修改诊断输出 schema、报告规则、检索、权限或调查额度。

## 改动契约

- 新调查使用 `investigation-context-v3.2-probe-view` / `structured_probe_view`。实际调用仍为 system/user messages + 原有 Decision JSON schema；user message 仍是 JSON 字符串，但 `evidence[].data` 直接为对象，不再把整份正文序列化进 `excerpt` 字符串。
- 仅识别字段恰为 `service/message`、内容为 `GET /readyz` 或 `/livez` 标准访问日志的行。去掉合法 Kubernetes 时间前缀后，文本完全相同且重复的行才能分组。状态码、路径、服务或原文字节不同不合并；新增字段、重复 JSON 键、异常格式不分组。
- 分组包含原文样例、次数、首末时间、原始 1-based 行号范围；其他行通过 `raw_spans` 完整保留。依赖失败日志（含内部时间、异常类型、目标）、未知日志不做语义摘要。逐次探针时间仍保存在原始证据，模型只见该组首末时间。
- 分组比原文更大时回退到原文。没有扫描/分组数量截断，也没有新增输入额度。采样仍为最近 1000 行、最多 256 KiB；计数不代表完整历史。
- 保存证据、页面 2000 字符预览、按需正文及旧导出不变。`last-llm` 保存实际新输入；账本记录正文 hash、来源 hash 和日志表示字节数。`brief` 中证据卡片的省略不等于模型省略。
- 通过现有账本固定每个 run 的输入版本：已调用过 v3.1 的调查继续旧格式，新调查使用新格式。未知/冲突版本明确拒绝，不假装兼容重放。

涉及 `log_projection.py`、`working_context.py`、`context.py`、`model.py`、`graph.py`，相应调查测试及只读比较脚本。不新增模型、数据库或集群调用。

## ECS 自动验收

项目根目录、Python 3.12 虚拟环境中执行；先确认无未提交冲突：

```bash
git pull --ff-only origin feature/baseline-contracts
git rev-parse HEAD
git status --short
bash scripts/accept_stage6bc2.sh
```

预期 `PASS: 6B-C2 ECS acceptance.` 且无 skipped。该脚本自动包含新增 `test_probe_projection.py`，使用隔离 PostgreSQL、受控模型/集群；覆盖异常原文保留、精确分组/行号、1000 行采样、不增大输入、JSONB 稳定性、旧格式付费重放、新格式实际保存、引用/权限/调查回归。不能代替真实模型验收。

用上次 Max 的旧导出做同一份证据的表示比较（将路径替换为该文件在 ECS 的实际位置）：

```bash
python -m scripts.compare_investigation_context \
  --last-llm evals/results/investigation-debug/20261011T134330Z-f5a5b260-last-llm.json
```

预期 `evidence_ids_unchanged=true`、证据序列化字节数下降；日志仍覆盖原来的 546 行，230 条依赖失败日志逐条保留，探针行以分组或原文表示。其他启动日志也必须保留。比较仅替换保存输入中的证据表示，不调用模型、不补造原始数据库证据；字符/字节下降不是实际 token 测量，不能证明诊断质量。

## 部署与真实验收

结束当前活动调查后执行：

```bash
bash scripts/deploy_stage6b2b_backend.sh
```

无新配置、依赖或迁移。更新 backend/worker；现有 Compose 挂载后端目录，无需重建后端镜像。前端和 Demo 镜像不变，无需重建。回滚前结束新格式调查，不能在进行中的 run 上随意降级代码。

若故障仍存在可直接创建新事件；若已恢复，再执行：

```bash
bash scripts/stage5_fault.sh dependency_unavailable
```

使用同一模型配置，新建 `agent-demo/order-service` 调查，描述只写实际症状。结束后：

```bash
python -m scripts.export_investigation_debug --incident-id <事件ID> --run-id <任务ID> --brief
python -m scripts.export_investigation_debug --incident-id <事件ID> --run-id <任务ID> --last-llm
```

必须人工核对：

1. 新 run 使用 v3.2；实际 user message 中证据是对象，分组来源和原文片段可对回已保存正文，依赖失败记录仍在。
2. 模型继续引用这些证据，区分观察到的依赖访问失败与未验证下游根因；无错误配置修复、无无据“已恢复”。没有关键证据时诚实保留 unknown；不因计数多提升根因确定性。
3. 报告症状、假设、缺失证据和下一步仍有用，不能仅以 schema 通过判成功。只采一个 Pod 时不能宣称逐一验证全部副本原因。
4. 记录实际 input_tokens、调用数、采样行数及报告内容。不同采样/调用数的 run 不作严格成本归因；同证据比较负责说明表示缩减，真实新事件负责说明诊断有效。

取证完成后按需 `bash scripts/stage5_fault.sh reset`。本地仅 AST/差异/代码契约静态检查；未执行 pytest、数据库、模型或集群。真实缩减比例与模型效果待上述 ECS 验收，不预填结果。
