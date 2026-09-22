# V1-005: Bounded Executor、Evidence Extraction 与持久化

- Status: Planned
- Phase: V1
- Depends on: V0-004, V1-002, V1-004

## 目标

实现顺序、有界、可审计的 Executor：执行已验证计划，应用预算和分类重试，记录每次 Tool 尝试，并将已验证 ToolResponse 确定性转换为追加式 Evidence。

## 上下文

- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/tool-protocol.md`
- `docs/architecture/evidence-model.md`
- `docs/architecture/error-taxonomy.md`
- `docs/architecture/storage-model.md`
- `docs/architecture/v0-contract-baseline.md`

## 架构位置

```text
Validated ExecutionPlanV1
  -> Executor
       -> Budget / AgentState
       -> ToolRegistry.invoke
       -> ToolCallRepository
       -> EvidenceExtractorRegistry
       -> EvidenceRepository
```

Executor 不解释业务根因。Evidence Extractor 是按 Tool output 类型注册的纯确定性映射器，不调用 LLM。

## 执行语义

1. 要求 State 处于 PLANNING，并转换为 EXECUTING。
2. 对每个 step：
   - 执行前检查步骤、Tool call、重试和总耗时预算。
   - 构造 `ToolInvocation` 并调用 Registry。
   - 每次尝试都记录独立 Tool Call 记录。
   - 失败时读取 `ErrorInfo` 和 Tool RetryPolicy 决定是否重试。
   - 成功后运行对应 Evidence Extractor 并追加 Evidence。
3. 计划结束后返回 Execution Summary 和更新后的 State。
4. 任一边界错误都保留已有 Tool Call 与 Evidence，不回滚历史事实。

V1 顺序执行，不并发。重试继续使用同一 logical call ID，并为存储记录生成独立 attempt record ID/sequence。

## Storage 演进

为保证重试可查询，新增 Alembic revision 扩展 `tool_calls`：

```text
logical_call_id
attempt_no
```

- 约束 `run_id + logical_call_id + attempt_no` 唯一。
- 既有数据迁移为 `logical_call_id=id`、`attempt_no=1`。
- `sequence_no` 继续表示 Run 内所有实际尝试的全局顺序。
- Evidence 的 `tool_call_id` 指向产生该 Evidence 的成功 attempt record ID，不指向抽象 logical ID。
- 禁止修改首个 migration；upgrade/downgrade 和 ORM 漂移测试必须同步增加。

## Repository 边界

定义 Protocol 而不是在 Runtime 中操作 SQLAlchemy Session：

```text
TaskRepository
RunRepository
LlmCallRepository
ToolCallRepository
EvidenceRepository
ResultRepository
```

提供 SQLAlchemy 实现和测试用 in-memory/fake 实现。事务以“一次调用记录 + 对应 Evidence”作为最小提交单元；Evidence 插入后不可更新。

## Evidence Extractor

V1 至少实现：

- Pod Status -> restart count、CrashLoopBackOff、last termination。
- Pod Events -> Liveness failure、Killing/BackOff。
- Previous Logs -> 声明启动时长与终止前未 Ready。
- Deployment -> Liveness 时间参数、Startup Probe 是否存在。
- Current Logs -> 有内容时生成日志 Evidence，但首个 Case 不要求。

Extractor 输入是经过 output model 验证的 Tool 数据，输出使用现有 `Evidence`。`source_confidence` 对 Kubernetes API 直接事实为 1.0；Extractor 不设置推断置信度。

Evidence ID、Tool attempt ID、时间和 clock 必须可注入，保证测试确定性。Evidence 必须绑定当前 Trace 和成功的 Tool attempt。

## 预算与重试

- 每次实际 Registry 调用计入 `tool_calls_used`，包括失败和重试。
- 每次重试计入 `retries_used`。
- 执行前和执行后更新 elapsed time。
- 预算不足时不发起下一次调用，返回 `BUDGET_EXCEEDED`。
- 只有 Error Taxonomy 与 Tool RetryPolicy 同时允许时才能重试。
- 不对 `INVALID_ARGUMENT`、`PERMISSION_DENIED`、`POLICY_REJECTED` 或非法输出重试。

## 失败状态

- 无任何可用 Evidence 的不可恢复失败：FAILED。
- 已有部分 Evidence、后续步骤失败：由上层在 V1-006 决定 PARTIAL。
- 预算耗尽：BUDGET_EXCEEDED，保留 Execution Summary。
- Risk 非 0 理论上应由 Validator 拒绝；Executor 仍做防御检查。

## 测试策略

- 正常四步计划产生 Tool Call 和四类 Evidence。
- timeout 后一次合法重试、非重试错误、重试预算耗尽。
- Tool call count 在成功/失败/重试路径准确。
- Evidence Trace/Tool Call 关联和追加写约束。
- Repository 事务失败不产生孤立 Evidence。
- 使用临时 SQLite 验证 Trace 可查询所有调用与 Evidence。
- 迁移测试验证 retry 字段回填、唯一约束、upgrade/downgrade 和 ORM 无漂移。
- Fake Registry 验证 Executor 从不直接调用 Handler。

## 不做

- 不生成 Root Cause、Recommendation 或 Verification。
- 不动态修改计划或并行调用 Tool。
- 不实现队列、分布式锁、缓存或长期 raw payload 对象存储。
- 不吞掉错误后无差别重试。

## 验收条件

- [ ] 只有 Validated Plan 可以进入 Executor。
- [ ] 每次 Tool 尝试可按 Trace 查询，失败尝试也被记录。
- [ ] logical call、attempt number 与成功 Evidence 的具体 Tool Call 关联清晰。
- [ ] 预算与 RetryPolicy 同时约束重试和后续调用。
- [ ] 四类 required Evidence 可从 V0 Case 响应确定性生成。
- [ ] Evidence 只追加且与当前 Run/Tool Call 外键一致。
- [ ] 错误使用统一 Taxonomy，已有证据不因后续失败丢失。
- [ ] 单元测试、数据库集成测试和 strict mypy 通过。
