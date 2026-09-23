# Agent State

## 目的

`AgentState` 是一次诊断的唯一运行时状态载体。它支持状态转换、持久化、恢复、Trace 和 Offline Replay，不允许通过隐藏的进程内变量保存关键事实。

## 建议字段

```python
class AgentState:
    task_id: str
    trace_id: str
    user_query: str
    intent: IntentOutput | None
    plan: list[PlanStep]
    current_step: int
    tool_call_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    diagnosis_id: str | None
    verification_id: str | None
    budget: BudgetState
    status: AgentStatus
    created_at: datetime
    updated_at: datetime
```

具体实现应使用 Pydantic 模型；字段名可以在 V0 实现时调整，但调整必须同步 Schema、测试和本文档。

## 状态枚举

```text
CREATED
ROUTING
PLANNING
EXECUTING
VERIFYING
WAITING_APPROVAL
COMPLETED
PARTIAL
FAILED
BUDGET_EXCEEDED
POLICY_REJECTED
```

## 状态约束

- 终止状态为 `COMPLETED`、`PARTIAL`、`FAILED`、`BUDGET_EXCEEDED` 和 `POLICY_REJECTED`。
- 终止后的运行不得继续调用 Tool。
- 每次状态转换必须更新时间并写入 Trace。
- 状态快照不可变；转换返回一个包含 `StateTransition` 记录的新快照。
- V0 使用 ID 引用尚未实现的 Tool Call、Evidence、Diagnosis 和 Verification 实体。
- 超出预算时停止探索，可使用已有 Evidence 生成 `PARTIAL` 结果。
- V1 中预算终止的 Run 状态保持 `BUDGET_EXCEEDED`；若已有 Evidence，可在不新增模型或 Tool 调用的情况下持久化状态为 `PARTIAL` 的诊断结果。
- V1 不应进入需要真实写操作的审批执行流程；`WAITING_APPROVAL` 为后续阶段预留。

## 状态转换表

| 当前状态 | 允许进入 |
|---|---|
| `CREATED` | `ROUTING`、`FAILED`、`POLICY_REJECTED`、`BUDGET_EXCEEDED` |
| `ROUTING` | `PLANNING`、`FAILED`、`POLICY_REJECTED`、`BUDGET_EXCEEDED` |
| `PLANNING` | `EXECUTING`、`PARTIAL`、`FAILED`、`POLICY_REJECTED`、`BUDGET_EXCEEDED` |
| `EXECUTING` | `PLANNING`、`VERIFYING`、`WAITING_APPROVAL`、`PARTIAL`、`FAILED`、`POLICY_REJECTED`、`BUDGET_EXCEEDED` |
| `VERIFYING` | `EXECUTING`、`COMPLETED`、`PARTIAL`、`FAILED`、`POLICY_REJECTED`、`BUDGET_EXCEEDED` |
| `WAITING_APPROVAL` | `EXECUTING`、`PARTIAL`、`FAILED`、`POLICY_REJECTED`、`BUDGET_EXCEEDED` |
| 任意终止状态 | 无 |

`PLANNING -> EXECUTING -> PLANNING` 支持根据新 Observation 动态调整计划；`VERIFYING -> EXECUTING` 支持补充缺失证据。代码与测试是该转换表的最终依据。
