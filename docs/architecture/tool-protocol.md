# Tool Protocol

## 定义

所有外部访问能力必须先注册为 Tool。Planner 只能引用注册表中存在的工具名称，Executor 只能执行验证通过的调用。

每个 Tool Definition 至少包含：

```text
name
description
risk_level
input_model
output_model
timeout_seconds
retry_policy
version
```

## 调用流程

```text
Tool Request
  -> Registry Lookup
  -> Input Schema Validation
  -> Policy and Permission Check
  -> Budget Check
  -> Timed Execution
  -> Output Validation and Normalization
  -> Trace Record
  -> Evidence Extraction
```

## 标准响应

```json
{
  "success": true,
  "data": {},
  "metadata": {
    "source": "kubernetes",
    "duration_ms": 123,
    "tool_version": "v1"
  },
  "error": null
}
```

失败响应必须携带统一错误类型，不向 Planner 暴露未清洗的内部异常或凭据。

## V1 工具范围

```text
k8s.get_pod_status
k8s.get_pod_events
k8s.get_pod_logs
k8s.get_previous_logs
k8s.get_deployment
```

这些工具全部为 Risk 0，只允许读取明确 namespace 中符合 Kubernetes 命名规则的资源。禁止注册任意命令执行工具。

