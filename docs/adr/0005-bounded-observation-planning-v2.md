# ADR 0005: V2 采用有界观察驱动的多轮规划

- Status: Accepted
- Date: 2026-09-23

## Context

V1 的 `ExecutionPlanV1` 一次生成并封存整条计划，`PlanValidator` 只
允许五个 Kubernetes Tool 和 `pod_restart`，`BasicCrashLoopVerifier`
只验证一个根因。V2 需要在观察到 OOM、无 Endpoint 或发布后错误率变化
时改变下一步路径。直接放宽 V1 类型或让模型自己循环会破坏冻结契约、
审计和预算边界。

## Decision

引入独立版本的 `IntentV2`、`PlanDecisionV2`、
`ObservationSummaryV2` 和 `CaseDefinitionV2`。Runtime 按轮执行
`Planner -> Validator -> Registry -> Evidence`，每轮只批准少量
Risk 0 调用，并由代码从当前 Trace Evidence 构造下一轮观察摘要。
模型输出使用 ADR 0004 的 strict wire 封套，内层仍经 Pydantic 和
语义校验；模型提出结束并不绕过确定性 Verifier。

全 Run 共用现有步骤、Tool、重试、Token、成本与总耗时预算，并另加
显式最大轮数；无新 Evidence、重复观察或预算耗尽时停止并保留已有
Evidence，按规则输出 Partial 或稳定失败。每轮决策、Evidence 快照
和版本需可审计。V1 计划、Case、Replay 和结果解释保持不变；必要
持久化只做 additive migration。

## Consequences

- 能用同一初始症状验证不同观察导致不同 Tool 路径，避免固定流程。
- 新增轮次、无进展检测、跨轮 call ID 去重和预算测试；模型调用及
  延迟可能增加，因此默认轮数和单轮调用数必须保守。
- 多数据源输出仍是不可信数据，不能变成控制指令；任何外部访问继续
  经注册 Tool、Schema、Policy、权限和预算检查。
- 生产可用性、自动修复、任意查询语言和写权限不在本决策范围内。
