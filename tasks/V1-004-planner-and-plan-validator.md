# V1-004: Planner 与 Plan Validator

- Status: Planned
- Phase: V1
- Depends on: V1-002, V1-003

## 目标

实现带显式 Tool 参数的 V1 结构化执行计划和独立 Plan Validator，使 Planner 只能从 Registry Descriptor 白名单中选择受控 Tool，并在执行前完成参数、风险与预算检查。

## 上下文

- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/tool-protocol.md`
- `docs/architecture/v0-contract-baseline.md`
- ADR 0001、ADR 0003

## Schema

新增而不修改 V0 `PlanStep` 语义：

```text
ExecutionPlanV1
  schema_version: Literal[1]
  steps: tuple[ExecutableStepV1, ...]

ExecutableStepV1
  step_id: sequential int
  call_id: unique runtime ID
  tool: registered Tool name
  arguments: JSON-only object
  reason: bounded text
```

最多 8 步，call ID 唯一，步骤从 1 连续编号。Plan 是声明，不包含 SDK 对象、Handler 或 Python callback。

## Planner 范围

1. 输入 Intent、五个 Tool Descriptor、V1 故障范围和预算摘要。
2. 使用 `StructuredModelClient` 产生 `ExecutionPlanV1`。
3. 对 CrashLoopBackOff 期望选择：
   - Pod Status。
   - Pod Events。
   - Previous Logs。
   - Deployment。
4. Current Logs 可用但不是首个 Case 的必选步骤。
5. 每一步必须携带 namespace、workload/pod 和必要 container 参数。
6. Prompt 明确 Tool 输出是不可信 data，Planner 不执行其中指令。

## Plan Validator

Validator 是纯确定性组件，执行：

- Tool 名称必须存在于当前 Registry Descriptor 集合。
- Risk 必须为 READ_ONLY。
- arguments 使用该 Tool input model 严格校验。
- namespace 必须与 Router Target/API scope 一致。
- 步数、预计 Tool calls、重试上限与 Runtime Budget 兼容。
- 不允许重复的相同 Tool + arguments，除非计划显式声明合法理由；V1 默认拒绝。
- 不允许调用第五个范围以外的 Tool。
- 不允许自由 label selector、shell 字段或未声明参数。

Validator 返回不可变 Validated Plan 或稳定 `ErrorInfo`，不得静默删除/修正危险步骤。

## 失败与 regeneration

- 模型 Schema 无效：按 `SCHEMA_VALIDATION` 最多 regeneration 2 次。
- Schema 合法但 Tool/参数/风险无效：给 Planner 一次受控 regeneration；重复失败终止。
- Budget 超限：`BUDGET_EXCEEDED`，不 regeneration。
- 未知 Tool/Risk 非 0：`POLICY_REJECTED` 或 `TOOL_NOT_FOUND`，记录安全事件。

所有 Planner 尝试写入 `llm_calls`；最终接受的 Plan 写入 Agent State。

## 测试策略

- ScriptedModelClient 产生正确计划并通过五 Tool descriptors 校验。
- 未知 Tool、Risk 1/2、namespace 越界、自由 selector、额外参数。
- 非连续 step、重复 call ID、重复调用、9 步计划和预算不足。
- 一次无效后 regeneration 成功；超过上限失败。
- Descriptor 顺序变化不改变验证结果。
- V0 Plan 反序列化测试继续通过，证明无破坏性修改。

## 不做

- 不执行 Tool。
- 不动态重规划或根据中间 Observation 修改计划。
- 不让 Planner 生成 Evidence、Root Cause 或 Kubernetes selector。
- 不支持 V1 单故障范围外的 Tool。

## 验收条件

- [ ] `ExecutionPlanV1` 版本化且不破坏 V0 Plan。
- [ ] Planner 只能看到 ToolDescriptor，不能看到 Handler/SDK。
- [ ] Validator 在执行前拒绝未知、越权、非法参数和超预算计划。
- [ ] V0 Case 产生包含 Status、Events、Previous Logs、Deployment 的计划。
- [ ] 所有尝试和最终 Plan 可追踪。
- [ ] 单元测试、Prompt 快照测试和 strict mypy 通过。
