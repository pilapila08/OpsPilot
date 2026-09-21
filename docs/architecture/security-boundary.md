# 安全边界

## 信任模型

LLM、用户输入、日志、指标、代码、RAG 文档和所有外部 Tool 输出均视为不可信。只有经过代码实现的 Schema、Policy、权限检查和预算检查可以授权执行。

## 风险等级

| 等级 | 示例 | 策略 |
|---|---|---|
| Risk 0 | 状态、日志、指标、事件、配置读取 | 验证后自动执行 |
| Risk 1 | 重启、扩缩容、回滚 | 必须人工审批并使用独立权限 |
| Risk 2 | 删除 PVC/namespace、修改网络策略、数据库写入 | 默认禁止 |

## V1 权限边界

- Tool Gateway 使用只读 Kubernetes Service Account。
- 仅授予必要资源上的 `get`、`list`、`watch`。
- LLM 运行环境不接触 kubeconfig 或云凭据。
- namespace 和资源名必须通过严格 Schema 校验。
- Tool 输出进入模型前需标记为数据，并限制大小、类型和敏感字段。
- Trace 不记录密钥、认证头或未经处理的敏感环境变量。

## 必测攻击面

```text
不存在的 Tool
路径或资源名注入
日志中的 Prompt Injection
Risk 1 未审批调用
Risk 2 调用
预算耗尽后的继续调用
权限不足和超时
模型输出 Schema 错误
```

