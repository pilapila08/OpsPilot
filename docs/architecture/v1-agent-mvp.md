# V1 Agent MVP Architecture

## 目标

V1 只完成一条可审计诊断链：对 `CrashLoopBackOff + Liveness Probe 配置过早` 进行 Router、Planner、Executor、Evidence、基础 Verifier 和 API 的端到端诊断。

V1 验收结果必须包含：

- 结构化 Intent 和执行计划。
- 五个可运行的只读 Kubernetes Tool。
- 每次 LLM 与 Tool 调用的 Trace 记录。
- 绑定 Evidence ID 的 Root Cause、Recommendation 和 Verification。
- Live Kubernetes 与固定 Offline Replay 共用同一 Runtime。

## 不做

- 不支持第二类故障或动态重规划。
- 不引入 RAG、MCP、Multi-Agent、Redis、前端或写操作。
- 不实现高级冲突推理、长期原始结果归档或分布式 Worker。
- 不允许 LLM 直接访问 Kubernetes SDK、kubeconfig、SQLAlchemy Session 或任意 Shell。

## 组件链路

```text
POST /diagnosis
  -> DiagnosisService
  -> Runtime Orchestrator
       -> Router -> StructuredModelClient
       -> Planner -> StructuredModelClient
       -> Plan Validator
       -> Executor
            -> Budget / State Transition
            -> Tool Registry
                 -> Kubernetes Tool
                      -> KubernetesReader
                      -> CoreV1Api / AppsV1Api
            -> Tool Call Repository
            -> Evidence Extractor
            -> Evidence Repository
       -> Diagnosis Assembler
       -> Basic Verifier
       -> Result Repository
  -> GET /diagnosis/{task_id}
```

每个箭头只传递 Pydantic Schema 或显式 Repository/Protocol，不传递 SDK 对象、数据库 Session 或自由文本控制指令。

Structured Model 与 Router 的 V1 具体契约见 [Model Gateway and Intent Router V1](model-gateway-v1.md)。

## 模块边界

| 模块 | 输入 | 输出 | 关键约束 |
|---|---|---|---|
| StructuredModelClient | Prompt version、消息、目标 Schema、调用预算 | 通过 Schema 校验的模型对象 | 最多 2 次 Schema regeneration；记录调用 |
| Router | 用户 query、显式 namespace | `IntentOutput` | V1 只接受 diagnose + Kubernetes restart 场景 |
| Planner | Intent、Tool Descriptor 白名单、V1 Case 范围 | `ExecutionPlanV1` | 显式 Tool 参数；最多 8 步；不生成未知 Tool |
| Plan Validator | Plan、Registry descriptors、Budget | Validated Plan | 参数、Risk、重复调用、上限和目标一致性 |
| Executor | Validated Plan、AgentState | ToolResponse、Evidence、更新后的 State | 顺序执行；所有调用经 Registry；分类错误与有界重试 |
| Evidence Extractor | 已验证 ToolResponse、Invocation、Trace | 0..N Evidence | 纯确定性映射；不调用 LLM |
| Diagnosis Assembler | Intent、Evidence | Candidate Claim + Recommendation | 只引用当前 Trace 的 Evidence |
| Basic Verifier | Claim、Evidence、Case 规则 | Verification | 规则优先；不足返回 Partial |
| Repository | 领域模型 | 数据库记录 | 保持 Trace/Run 外键；Evidence 只追加 |

## V1 新增 Schema

V0 `PlanStep` 不携带 Tool arguments。为遵守 V0 冻结，V1 新增独立执行 Schema：

```text
ExecutionPlanV1
  schema_version = 1
  steps: tuple[ExecutableStepV1, ...]

ExecutableStepV1
  step_id
  call_id
  tool
  arguments
  reason
```

`arguments` 必须是 JSON-only，且在计划校验时使用 Registry 中该 Tool 的 input model 验证。不要修改 V0 `PlanStep` 的既有含义。

候选诊断使用现有 `Claim` 和 `Verification`。API DTO 与存储 ORM 分离，禁止直接序列化 SQLAlchemy 对象作为外部响应。

## 运行时状态

正常路径：

```text
CREATED -> ROUTING -> PLANNING -> EXECUTING -> VERIFYING
        -> COMPLETED
        -> PARTIAL
```

失败路径：

- Schema 仍不合法：`FAILED` + `SCHEMA_VALIDATION`。
- Tool/LLM 预算耗尽：`BUDGET_EXCEEDED`，保留已有 Evidence 并返回 Partial。
- Risk/Policy 拒绝：`POLICY_REJECTED`。
- Tool 失败且不允许重试：`FAILED` 或在已有足够 Evidence 时 `PARTIAL`。
- Evidence 不足或矛盾：`PARTIAL`，不得输出确定性 Root Cause。

每次状态变化使用现有状态转换 API，禁止直接替换 `status` 字段。

## Live 与 Offline Replay

`ToolRegistry` 和 Runtime 不区分 Live/Replay。两种模式只替换 Tool Handler 的依赖：

- Live：Kubernetes Handler 使用 `KubernetesReader` 调用官方 Python SDK。
- Replay：Handler 按 `call_id` 从 `LoadedCase` 返回已验证 ToolResponse。

两种模式必须使用相同 Tool 名称、input/output Schema、Evidence Extractor 和 Verifier。Replay 不得绕过 Plan Validator、Budget 或 Evidence 绑定。

## 持久化顺序

1. 创建 `diagnosis_tasks` 和 `agent_runs`。
2. 每次模型调用完成后追加 `llm_calls`。
3. 每次 Tool 尝试完成后追加 `tool_calls`，包括失败尝试。
4. 成功响应经 Extractor 生成 `evidence`；原始响应先保存在 Tool Call 的 result payload。
5. Verifier 后写入唯一 `diagnosis_results`。
6. 最终更新 Run 与 Task 状态和完成时间。

V1 在同一进程中顺序提交即可，不引入消息队列。Repository 负责事务边界，Runtime 不直接拼写 SQL。

## 配置与安全

- LLM provider、model、timeout、Prompt version 和预算来自配置，不硬编码。
- Kubernetes 使用 in-cluster config 或显式开发 context；凭据不进入模型、日志或 Trace。
- 五个 Kubernetes Tool 均为 Risk 0，输入只允许合法 namespace 和资源名。
- 日志、Events、环境变量和所有外部输出是不可信数据，必须限长并作为 data 传入模型。
- 敏感环境变量、Secret 引用、认证头和 SDK 异常正文必须清洗。

## V1 依赖顺序

```text
V1-001 Kubernetes Client Boundary
  -> V1-002 Five Read-only Kubernetes Tools

V1-003 Structured Model Client + Router
  -> V1-004 Planner + Plan Validator

V1-002 + V1-004
  -> V1-005 Executor + Evidence Persistence
  -> V1-006 CrashLoop Diagnosis + Basic Verifier
  -> V1-007 Runtime Orchestration + Offline E2E
  -> V1-008 FastAPI + Live Acceptance
```

## 阶段验收

- 五个 Tool 的输入、输出、错误映射、限长和只读属性有单元测试。
- Router/Planner 的正常、Schema regeneration、未知 Tool 和预算路径可测试。
- V0 Case 在 Replay 模式产生与 Ground Truth 一致的 Claim、Evidence 和 Verification。
- 数据库中可按 Trace 查到 LLM Calls、Tool Calls、Evidence 和 Result。
- API 能创建和查询任务；未知任务和失败状态使用稳定响应。
- 可选真实集群 smoke test只读取 fixture namespace，并可在无集群 CI 中跳过。
