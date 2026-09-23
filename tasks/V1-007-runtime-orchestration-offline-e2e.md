# V1-007: Runtime Orchestration 与 Offline E2E

- Status: Done
- Phase: V1
- Depends on: V1-004, V1-005, V1-006

## 目标

组合 Router、Planner、Validator、Executor、Diagnosis 和 Verifier 为单次有界 Runtime，并使用 V0 Case 完成不依赖 Kubernetes 或付费 LLM 的端到端 Offline Replay。

## 上下文

- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/agent-state.md`
- `docs/architecture/storage-model.md`
- `fixtures/cases/crashloop-liveness-v1/`
- `docs/architecture/v0-contract-baseline.md`
- `docs/architecture/diagnosis-v1.md`
- `docs/architecture/runtime-v1.md`

## Runtime 接口

定义应用层入口：

```text
DiagnosisRequest
  query
  namespace
  mode: live | replay
  case_id: optional

DiagnosisRunResult
  task_id
  run_id
  trace_id
  status
  diagnosis / error
```

`DiagnosisRuntime.run` 接收已构造依赖，不读取全局环境。ID、clock、Model Client、Tool Registry、Repository 和 Case Loader 全部可注入。

## 编排顺序

1. 创建 Task、Run 和初始 AgentState。
2. ROUTING：调用 Router 并持久化 Intent/LLM Call。
3. PLANNING：调用 Planner 与 Plan Validator。
4. EXECUTING：运行 Executor，持久化 Tool Calls/Evidence。
5. VERIFYING：生成 Draft、运行 Basic Verifier、写 Result。
6. 完成：
   - 证据充分 -> COMPLETED。
   - 证据缺失/冲突或部分 Tool 失败 -> PARTIAL。
   - 无法进入验证 -> 对应失败终态。

每个阶段只通过显式返回值和 AgentState 交接，不使用可变全局上下文。

## Replay Adapter

- 从 `CaseDefinition.replay_steps` 建立与五 Tool Definition 相同的 Handler。
- 每一步验证 Tool 名、arguments、phase 和顺序，再返回已验证 ToolResponse。
- call ID 可由 Scripted Planner 固定或通过显式映射关联，不能只按“第几个响应”盲目返回。
- Recovery step 不进入故障诊断 Evidence 集合。
- Replay 仍执行 Registry、Validator、Budget、Extractor、Verifier 和 Repository。

## 数据库 E2E

测试使用 Alembic 升级后的临时 SQLite：

- 一条 Diagnosis Task。
- 一条 Agent Run 和唯一 Trace。
- Router、Planner、Diagnosis Draft 的 LLM Calls。
- 四个或计划要求的 Tool Calls。
- 四类 Evidence。
- 一条 COMPLETED Diagnosis Result。

按 Trace 读取的记录必须能重建阶段顺序、Tool 版本、Prompt 版本和最终 Evidence 引用。

## 可恢复失败场景

- Router Schema 重试后失败。
- Planner 生成未知 Tool。
- 第二个 Tool timeout 后预算耗尽。
- Previous Logs 缺失导致 Partial。
- Repository 写入失败导致 Run FAILED，但已提交 Evidence 不被篡改。

V1 不实现进程崩溃后自动恢复；数据库必须留下可诊断状态，恢复机制推迟到 V3。

## 测试策略

- 完整 V0 Case -> COMPLETED 且与 Case Ground Truth 一致。
- 删除一个响应/Evidence -> PARTIAL。
- Replay 顺序、参数或 Tool 名不匹配 -> 明确失败。
- 预算上限覆盖所有阶段，并验证没有超限后的额外调用。
- 状态时间线只出现合法转换。
- 数据库 Trace 关联完整，Evidence 仍追加写。
- 同一个 Case 连续运行两次产生不同 Run/Trace，不共享可变状态。

## 不做

- 不暴露 HTTP API。
- 不运行真实 Kubernetes 或真实付费模型。
- 不实现队列、并发、动态重规划、断点恢复或分布式 Trace。
- 不绕过任何组件以让 E2E“直接返回预期答案”。

## 验收条件

- [x] 单一 Runtime 入口完成 Router 到 Result 的全链路。
- [x] Offline Replay 与 Live 使用同一 Tool/Executor/Verifier 契约。
- [x] V0 Case 产生 COMPLETED 且诊断与 Ground Truth 对齐。
- [x] Partial 与失败路径保留完整状态、调用和已有 Evidence。
- [x] 数据库可按 Trace 重建全部阶段。
- [x] 预算耗尽后不会发生新的模型或 Tool 调用。
- [x] E2E、迁移回归和 strict mypy 通过。

## 实施记录

- `DiagnosisRuntime` 以依赖注入组合 Router、Planner、Validator、Executor、Assembler 和 Repository；Task/Run、每阶段 AgentState 及合法终态均持久化。
- `ReplayRegistryFactory` 每次运行装配全新只读 Registry/Reader，严格核对 Case 的故障阶段顺序、call ID、Tool、namespace、Pod 与参数；恢复阶段不参与诊断。
- Alembic SQLite 全链路得到三次 LLM Call、四次 Tool Call、四类 Evidence 与匹配 Ground Truth 的 COMPLETED Result；Live 模式用注入 Reader 复用同一 Runtime。
- Router Schema 耗尽、未知 Tool、Replay 失配、Tool 重试预算、缺失 Previous Logs、Result 写入失败和双运行隔离都有 E2E 测试。预算耗尽且保留 Evidence 时只用确定性 Verifier 保存 Partial，不增加调用。
- 全量 271 项测试、111 个源文件 strict mypy 通过。
