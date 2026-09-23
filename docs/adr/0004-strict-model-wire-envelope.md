# ADR 0004: Strict Structured Output 使用扁平传输封套

- Status: Accepted
- Date: 2026-09-23

## Context

真实 Kubernetes Live smoke 已验证五个只读 Tool 和 Router，但 Planner 的
`ExecutionPlanV1` 被模型端点以 HTTP 400 拒收。SDK 生成的 strict Schema
包含动态 `arguments` 对象及空的 `JsonValue` 定义，不能满足 strict
Structured Outputs 的对象约束。下游 `DiagnosisDraftV1` 也包含嵌套
`$defs/$ref`，在未重跑 Live 前不应假定该端点能接收。

## Decision

OpenAI Adapter 对 `ExecutionPlanV1` 和 `DiagnosisDraftV1` 使用只有一个
`payload_json: string` 字段的 `StrictJsonEnvelope` 作为 provider-facing
Schema。Adapter 在返回领域对象之前，以严格 Pydantic JSON 解析原始字符串；
解析失败归类为 `SCHEMA_VALIDATION`，并保留 usage 供既有有界 regeneration
与审计使用。Planner 的 Tool 白名单、参数 Schema、Policy 和预算验证，以及
Diagnosis 的 Evidence 绑定和确定性 Verifier 均保持原样。其他模型输出仍直接
使用其领域 Schema。

Live Planner/Diagnosis Prompt 增加 v2，历史 v1 内容与 hash 不变。Replay
继续使用 v1；这只是 provider 传输层适配，不改变 V0 冻结契约或 V1 领域计划。

## Consequences

- 提交给 provider 的 strict Schema 是无动态属性、无嵌套引用的扁平对象。
- `payload_json` 内的 JSON 不由 provider Schema 保证有效，必须由现有领域
  Schema、Plan Validator 和 Verifier 在本地拒绝无效或越权内容；重试仍有预算上限。
- 本地 SDK Schema 与模拟 Live 全链路通过不等于真实端点通过；Live smoke 必须重跑。
