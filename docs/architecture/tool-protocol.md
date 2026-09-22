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

## V0 协议模型

`ToolInvocation` 包含：

```text
call_id
tool
arguments (JSON-only)
```

`ToolDefinition` 将可序列化描述与异步 Handler 绑定，注册时验证名称、风险等级、Pydantic 输入输出模型、超时、版本、来源与重试声明。Planner 只能读取不包含 Handler 的 `ToolDescriptor`。

`ToolRegistry.invoke` 当前只执行一次调用，顺序为：

```text
Whitelist Lookup
-> Risk 0 Check
-> Input Model Validation
-> Timeout-bound Async Handler
-> Output Model Validation
-> Normalized ToolResponse
```

重试策略在 V0-002 中只作为协议声明并标注错误是否可重试；真正的分类重试由后续 Executor 实现，Registry 不自行重试。

## V0 错误码

```text
TOOL_NOT_FOUND
INVALID_ARGUMENT
POLICY_REJECTED
TOOL_TIMEOUT
TOOL_EXECUTION_FAILED
TOOL_OUTPUT_INVALID
```

错误响应只包含稳定错误码、通用消息和 `retryable` 标记。Handler 的原始异常、参数内容和堆栈不会返回给 Planner。

## 风险执行规则

- Risk 0：允许 Registry 在验证后执行。
- Risk 1：定义可注册，但当前返回 `POLICY_REJECTED`，等待后续审批系统。
- Risk 2：定义可注册，但当前返回 `POLICY_REJECTED`，禁止真实执行。

## V1 工具范围

```text
k8s.get_pod_status
k8s.get_pod_events
k8s.get_pod_logs
k8s.get_previous_logs
k8s.get_deployment
```

这些工具全部为 Risk 0，只允许读取明确 namespace 中符合 Kubernetes 命名规则的资源。禁止注册任意命令执行工具。
