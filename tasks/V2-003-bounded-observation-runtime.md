# V2-003: 有界观察驱动 Planner 与 Runtime

- Status: Done
- Phase: V2
- Depends on: V2-002

## 目标

使 Planner 在执行一轮只读 Tool 后依据新 Evidence 选择不同下一步，
同时保持 V1 的调用审计、确定性校验和预算上限。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/adr/0005-bounded-observation-planning-v2.md`
- `docs/architecture/planning-v1.md`
- `docs/architecture/execution-v1.md`
- `docs/architecture/runtime-v1.md`

## 架构与实现

- 新增 `PlanDecisionV2`、`ObservationSummaryV2`、V2 Planner
  Prompt 与 Validator；继续用 strict wire/Pydantic 双层校验。
  `continue` 最多提四个新 Tool 调用，`finish` 仍须 Verifier。
  `finish`/`partial` 不得附调用；based-on Evidence ID 只能来自
  当前 Trace 已持久化的轮次快照。
- Runtime 管理最多四轮，复用合法状态转换，不覆盖 V1 sealed plan。
  全轮共用 steps/calls/retries/tokens/cost/time Budget；按实际尝试
  计数，剩余预算不足时拒绝整个候选轮或安全缩减规则须明确测试。
- 候选轮预算不足时整体拒绝，不静默删减模型提出的步骤；每轮持久化
  round number、Evidence ID 快照、decision hash、
  Prompt 版本与校验结果；新增字段/表只用 additive Alembic revision，
  旧 Run 可读。
- 跨轮 call ID 全局唯一；规范化 Tool 请求不可无意义重复。新 Evidence
  为零或观察签名重复时停止；已有 Evidence 可返回 Partial，零
  Evidence 则记录稳定失败，不伪造 Verification；模型不能无限自问。
- ObservationSummary 只由已持久化的当前 Trace 结构化 Evidence/
  Tool 错误生成，不传入原始日志或 diff 作为控制指令。
- 引入按故障族分发的确定性 V2 Verifier 接口，先用 Fake 规则证明
  finish/partial 边界，具体故障规则由后续工作单提供。

## 测试

- 同一初始症状在 OOM 与 probe 两种观察下产生不同 Tool 路径。
- 未知 Tool、Risk 1、跨 namespace、错目标、重复调用、无进展、
  Schema 错误与预算耗尽均不执行额外 Tool。
- 多轮 Trace 可重建；失败尝试与已有 Evidence 保留；Result 只能
  引用当前 Trace Evidence。
- V1 Offline E2E 与旧迁移回归保持通过。

## 不做

- 不加入真实外部数据源或具体八类根因规则。
- 不修改 V1 Planner/Verifier 的既有语义，不实现自动修复。

## 验收条件

- [x] 至少两个可复现分支证明观察真正改变计划。
- [x] 轮次、调用、模型和 Evidence 审计完整且预算有界。
- [x] 无进展时有证据返回 Partial、零证据稳定失败；Policy 拒绝保持安全终态。
- [x] Alembic、E2E、默认 pytest 与 strict mypy 通过。

## 完成记录

- PlanDecisionV2、ObservationSummaryV2、strict wire Planner 与精确输入模型/目标/预算 Validator 独立于 V1 sealed plan。四轮上限、每轮四调用上限，全 Run 共用预算。
- ObservationRuntimeV2 串联 V2 Router、轮次审计、只读 Executor、当前 Trace Evidence 与确定性 Verifier 接口；无新 Evidence、重复签名、预算/Policy 终止均有安全结果。
- additive 0004 migration 新增 planning_rounds；旧 V1 Run 保持可读。每轮保存决策 hash、Evidence 快照、Prompt 版本、action 和 call IDs；失败模型尝试保留于 llm_calls。
- SQLite E2E 在相同初始 Intent 下依据 OOMKilled/Error 结构化观察分别选择 Deployment/Events；V2 fake Verifier 仅输出 Partial，具体故障规则留给后续工作单。
- 默认 pytest：341 passed、1 skipped（opt-in Live）；strict mypy：138 source files。
