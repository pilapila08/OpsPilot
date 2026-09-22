# Model Gateway and Intent Router V1

## 目标

V1 使用 `StructuredModelClient` 隔离 Router/Planner 与模型供应商。上层只提交版本化 Prompt、消息、目标 Pydantic Schema 和外部化模型配置，得到已经验证的对象与 usage/latency/provider metadata，不接触供应商原始响应。

```text
IntentRouter / Planner
  -> StructuredModelClient
       -> ScriptedModelClient
       -> OpenAIStructuredModelClient
```

## 公共契约

`StructuredModelRequest` 包含有角色和长度边界的消息、Prompt component/version/hash，以及 provider、model、timeout、temperature、output token limit 和外部化价格。

`StructuredModelResult[T]` 包含已验证的 Pydantic output，以及 provider/model、response ID、latency、input/output token 和成本。供应商 SDK 对象、认证信息和原始异常不越过 Adapter。

`ScriptedModelClient` 与 Live Adapter 实现同一 Protocol。测试可以按顺序提供结构化 payload 或安全错误，并断言调用次数、目标 Schema、Prompt version 和完整请求。

## OpenAI Adapter

首个 Live Adapter 使用官方 OpenAI Python SDK `>=2.29,<3` 的 `responses.parse(..., text_format=TargetPydanticModel)`。

- API key、organization 和 project 只通过配置指定的环境变量名读取。
- model、timeout、temperature、token limit 和价格来自 `StructuredModelConfig`。
- SDK `max_retries=0`，避免未审计的隐式重试。
- 请求设置 `store=false`，不把 provider response ID 当作 OpsPilot 状态。
- rate limit、timeout、context、permission 和其他 API 错误转换为统一 Error Taxonomy；异常正文不进入 ErrorInfo 或审计 payload。

Provider Adapter 可以新增或替换，不改变 Router/Planner，因此本阶段不需要 provider 绑定 ADR。

## Prompt Version

Router Prompt 位于 `prompts/router/v1.md`。加载时按规范化 UTF-8 文本计算 SHA-256，`PromptTemplate` 验证 hash 与内容一致。

`ModelAuditRepository.register_prompt` 以 component + version 唯一注册 Prompt。相同版本若内容、hash 或 model family 不同则明确失败，禁止原地改写历史 Prompt。

## Intent Router

模型目标 `RouterModelOutput` 故意不包含 namespace，只产生 intent、domain、problem type 和 resource。最终 `IntentOutput.target.namespace` 始终由调用方显式 namespace 构造，query 中的 Prompt Injection 无法切换 namespace。

V1 只接受 `intent=diagnose`、`domain=kubernetes` 和 `problem_type=pod_restart`。Schema 合法但超出该范围的结果稳定返回 `INVALID_ARGUMENT`，不会扩大诊断能力。

## Regeneration 与预算

- 初次 Schema 失败后最多 regeneration 2 次，总尝试不超过 3。
- regeneration 消息只说明 Schema validation 失败，不重复用户 query 或无效模型原文。
- 只有 `SCHEMA_VALIDATION` 在 Router 内触发 regeneration；其他错误交给后续 Runtime 按 Taxonomy 处理。
- 每次尝试前检查 token、cost 和 time budget；每次 Schema retry 消耗 `retries_used`。
- 成功和可获得 usage 的失败尝试都会累计 token、cost 和 latency。

## 审计持久化

`ModelAuditRepository` 是 Router 与存储之间的边界。`SQLAlchemyModelAuditRepository` 复用冻结的 `prompt_versions` 和 `llm_calls` 表，不修改 V0 迁移。

每次尝试单独保存 run sequence、Prompt version、provider/model/version、usage、latency、retry count、结果状态与安全响应。请求只保存 namespace、query SHA-256、query length、Schema 名称和 attempt，不复制 query、凭据或供应商异常正文。

V1 Runtime 为单进程顺序执行；跨进程 sequence 分配留给后续持久化并发设计。
