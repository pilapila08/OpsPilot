# ADR 0007: 统一预算准入与可审计的 Partial

- Status: Accepted
- Date: 2026-09-25
- Depends on: ADR 0003、ADR 0006

## Context

原实现把预算判断分散在模型、Planner Validator、Executor 和两个 Runtime。部分模型失败消耗未写回 Run；V2 的预算拒绝可能被归类为 Policy 或外部错误；只有终态不能解释哪个维度阻止了哪次工作。冻结的 Evidence Verification 要求至少一条证据，无法如实表达 Router 后、Planner 前就停止的情况。

## Decision

1. `BudgetManager` 统一 steps / tool_calls / retries / tokens / cost / elapsed 的准入和不可变用量更新。模型与 Tool 调用前检查；Planner 前额外检查步骤和 Tool 剩余额度。零 retry 额度允许首次调用。
2. step 按逻辑 Tool 调用首次尝试计数，tool_calls 按每次实际尝试计数，模型重生成与 Tool 重试共享 retries。计划准入保留已有最坏调用数/超时投影。模型 usage 按成功或失败响应实际返回值累计，成本精度支持六位小数。
3. elapsed 使用本地单调时钟；provider latency 仅作审计指标。异步模型和 Tool 调用受剩余运行时间取消约束。同步存储及不合作的外部实现不能保证在截止瞬间被强制终止。
4. token/cost 调用前达到上限即拒绝；响应后大于上限立即停止。响应 usage 在调用前不可知，因此一次响应可能跨限；当前没有 token 预留或 provider 最大输出额度协调，不能将这些阈值宣传为精确的账单硬上限。
5. 新增独立 `budget_stops` 表保存每 Run 首次停止事实：维度、阶段、步骤、轮次、触发类型（已耗尽/投影超限/截止）、请求增量及六维用量/限制。相同记录可幂等追加，不允许覆盖为不同事实；旧 Run 不回填推测原因。Trace 通过 Run 归属读取它。
6. 预算停止后不再调用模型或 Tool。保留 Run 的 `BUDGET_EXCEEDED` 终态以及 Error Taxonomy；总是生成 `Result.status=PARTIAL`。新增 Result schema 3 和 `BudgetVerificationV3(kind=budget_stop)`，允许零 Evidence、零 Claim，明确 `supported=false` 和缺少完整诊断依据。已有 V1/V2 Evidence Verification 契约保持原语义。
7. 停止原因只通过本地 Trace CLI 提供，不增加 HTTP 运营数据接口。

## Consequences

- 零证据和有证据的预算终止都有可查询结果，且不会伪造根因或降低 Evidence 验证门槛。
- Run 终态与 Result 状态是不同口径：消费者应显示“预算终止 / Partial”，不能把预算错误当成无结果崩溃。
- 新表需要先执行 Alembic upgrade head。预算事实、Partial Result 和最终 Run 分别持久化；若进程或存储在这些边界失败，Trace 可能保留停止事实但尚无完整结果。当前不提供崩溃恢复事务编排。
- 后续精确成本预留、分布式额度和 provider 输出限制需要另立工作单。
