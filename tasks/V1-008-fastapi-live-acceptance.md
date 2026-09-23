# V1-008: FastAPI、Live Smoke Test 与 V1 验收

- Status: Ready
- Phase: V1
- Depends on: V1-007

## 目标

为已验证 Runtime 提供最小 FastAPI 接口，支持创建与查询诊断任务；完成可选真实 Kubernetes 只读 smoke test，并形成 V1 阶段验收报告。

## 上下文

- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/security-boundary.md`
- `docs/architecture/storage-model.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `fixtures/cases/crashloop-liveness-v1/README.md`
- `docs/architecture/runtime-v1.md`

## API

### POST /diagnosis

请求：

```json
{
  "query": "slow-start-api 为什么一直重启？",
  "namespace": "opspilot-fixtures",
  "mode": "replay",
  "case_id": "crashloop_liveness_v1"
}
```

返回 HTTP 202：

```json
{
  "task_id": "task_...",
  "status": "CREATED"
}
```

Live mode 不接受 case ID；Replay mode 必须提供已注册 Case ID。namespace 和 query 使用严格 DTO 校验。

### GET /diagnosis/{task_id}

返回任务状态以及在可用时的：

```text
run_id
trace_id
root_cause
evidence
recommendation
verification
error
```

未知 task 返回 404 稳定错误；内部异常、SQL、凭据和堆栈不得暴露。

## 调度模型

V1 使用有界的 in-process `RunDispatcher`：

- 生产实现通过 FastAPI lifespan 管理任务。
- 测试提供 Inline Dispatcher。
- POST 先持久化任务，再提交 Runtime。
- 同一 idempotency key 不重复创建任务。
- 应用关闭时不接受新任务并等待有限时间。

V1 明确不保证进程崩溃后的队列恢复；遗留 RUNNING 任务可被识别但自动恢复推迟到 V3。

## 配置

- 数据库 URL、Model provider/model、Kubernetes mode/context、timeout 与预算来自环境或配置文件。
- 启动时验证配置，但不建立不必要的集群写权限。
- Replay mode 可在无 LLM 凭据、无 Kubernetes 的环境运行。
- Live mode 缺少必要凭据时启动失败或明确禁用，不能静默降级到其他 namespace/provider。

## API 测试

- POST Replay -> 202 -> GET 最终 COMPLETED。
- GET 返回 Root Cause、四类 Evidence、Recommendation、Verification。
- idempotency、未知 task、非法 namespace、空 query、错误 mode/case 组合。
- Runtime FAILED/PARTIAL/BUDGET_EXCEEDED 的 HTTP 表达。
- 并发提交受配置上限约束，不产生重复 Run。
- 响应不包含 Tool 原始敏感字段、Prompt、凭据或堆栈。

## Live Smoke Test

使用 `opspilot-fixtures` namespace 和 V0 Case 清单：

1. 部署 broken manifest。
2. 使用只读 Service Account 运行五个 Tool 的必要调用。
3. 完成一次 Live Runtime 诊断。
4. 验证 Root Cause 与 required Evidence。
5. 应用 fixed manifest，验证 workload Ready。
6. 清理 fixture namespace。

测试标记为可选集成测试；无 Docker/Kubernetes 的默认 CI 跳过，但必须保留运行文档和最近一次人工验证记录。Smoke test 不执行任何由 Agent 发起的写操作；部署与清理由测试 harness/操作者完成。

## V1 验收报告

新增 `docs/roadmap/v1-acceptance.md`，逐项记录：

- FastAPI 可启动。
- Router 结构化输出稳定。
- Planner 选择正确 Tool。
- 五个 K8s Tool 可运行。
- CrashLoopBackOff E2E 成功。
- 所有 LLM/Tool Call 可记录。
- Evidence 输出完整。
- 结果包含 Root Cause/Evidence/Recommendation。

同时更新 `docs/STATUS.md`、任务索引和路线图。只有全部自动验收通过、live 未通过项明确记录时，才可将 V1 标记完成。

## 不做

- 不实现认证、租户、前端、WebSocket、SSE 或公开部署。
- 不引入 Celery、Redis、Kafka 或分布式 Worker。
- 不让 API 接受任意 Tool、Prompt 或 kubeconfig。
- 不由 Agent 自动应用修复清单。

## 验收条件

- [ ] POST/GET API 使用严格 DTO 并与 ORM 分离。
- [ ] Replay API 在无外部依赖环境完成端到端诊断。
- [ ] 调度有并发与关闭边界，幂等请求不重复运行。
- [ ] API 错误稳定且不泄漏内部数据。
- [ ] 可选 Live smoke test 和操作文档可执行。
- [ ] V1 原始验收项全部映射到自动测试或明确的人工检查。
- [ ] 完整 pytest、strict mypy 和项目状态更新通过。
