# Error Taxonomy

## 目的

所有运行时错误使用稳定 `ErrorCode`、所属 `ErrorCategory` 和默认 `ErrorAction`。调用方根据分类采取确定性行为，不依赖异常文本猜测是否应该重试。

## 默认策略

| ErrorCode | Category | Default Action | Retry | Max |
|---|---|---|---:|---:|
| `LLM_RATE_LIMIT` | LLM | `RETRY_WITH_BACKOFF` | 是 | 3 |
| `LLM_TIMEOUT` | LLM | `RETRY` | 是 | 2 |
| `SCHEMA_VALIDATION` | SCHEMA | `REGENERATE` | 是 | 2 |
| `TOOL_TIMEOUT` | TOOL | `RETRY` | 是 | 2 |
| `TOOL_NOT_FOUND` | TOOL | `FAIL` | 否 | 0 |
| `TOOL_EXECUTION_FAILED` | TOOL | `RETRY` | 是 | 1 |
| `TOOL_OUTPUT_INVALID` | TOOL | `FAIL` | 否 | 0 |
| `PERMISSION_DENIED` | PERMISSION | `FAIL` | 否 | 0 |
| `INVALID_ARGUMENT` | SCHEMA | `FAIL` | 否 | 0 |
| `POLICY_REJECTED` | POLICY | `ESCALATE_POLICY` | 否 | 0 |
| `BUDGET_EXCEEDED` | BUDGET | `RETURN_PARTIAL` | 否 | 0 |
| `CONTEXT_TOO_LONG` | CONTEXT | `REDUCE_CONTEXT` | 是 | 1 |
| `EXTERNAL_SERVICE_ERROR` | EXTERNAL | `RETRY_WITH_BACKOFF` | 是 | 2 |

## 约束

- 每个 ErrorCode 必须且只能有一条默认策略。
- 调用级策略可以关闭默认重试，此时 Action 收紧为 `FAIL`。
- 调用级策略不能把默认不可重试错误提升为可重试。
- Tool 的重试次数不能超过统一分类给出的默认上限。
- `ErrorInfo` 只保存稳定错误码、分类、Action、有效重试标记和脱敏消息。
- 原始异常、堆栈、凭据和未经清洗的参数不得进入 API 错误响应。
- V0-003 只定义策略；真正的重试循环由后续 Executor 实现。

