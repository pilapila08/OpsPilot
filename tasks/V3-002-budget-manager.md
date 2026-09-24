# V3-002: 六维 Budget Manager 与可审计停止原因

- Status: Done
- Phase: V3
- Depends on: V3-001

## 目标

将模型、Executor、V1/V2 Runtime 的预算判定统一，预算终止成为可查询、可回放的事实。

## 上下文

- `tasks/V3-001-trace-query.md`
- `src/opspilot/llm/budget.py`
- `docs/architecture/execution-v1.md`
- `docs/architecture/v2-diagnosis-expansion.md`

## 实现范围

- 统一 steps / tool_calls / retries / tokens / cost / elapsed 六维判定与用量更新；保留 V0/V1 BudgetState 序列化兼容性。
- 明确各阶段的准入规则：在模型/Tool 调用与 Planner 之前检查；零重试允许首调，但禁止重试。耗时使用 Run 墙钟口径，避免重复累计。
- 追加持久化 BudgetStop：trace/run、触发阶段、维度、步骤、当时六维用量和限制；旧 Run 不强制回填。
- TraceView 展示停止事实。预算结束后不再发起模型或 Tool，使用已有 Evidence 生成确定性 PARTIAL；即使零 Evidence 也返回可解释的 Partial。
- 记录模型返回后才可观察的 token/cost 超限；不宣称无法预知的响应用量可以提前精确控制。

## 验收

- [x] 六维边界、首调/重试区别、耗时口径有测试。
- [x] Planner 之前预算耗尽时不调用模型或 Tool。
- [x] V1/V2 超预算保留 Evidence 和 PARTIAL Result，停止原因经 Trace 可查询。
- [x] 新迁移升级/回滚、旧数据兼容、Trace 隔离通过。
- [x] 默认 pytest、strict mypy 通过，更新 STATUS；整体交接在 V3-003 收尾。

## 验证与决策

2026-09-25：默认 `425 passed, 4 skipped`；strict mypy `175 source files` 通过。停止事实迁移为 `20260925_0005`；运行终态仍为 BUDGET_EXCEEDED，Result 使用 schema 3 的无 Claim PARTIAL，包含零 Evidence 情况。详见 [ADR 0007](../docs/adr/0007-central-budget-and-audited-partial.md)。

## 不做

外部计费平台、分布式预算协调或新基础设施写权限。
