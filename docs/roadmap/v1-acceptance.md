# V1 Acceptance Report

- Date: 2026-09-23
- Result: Offline and Live accepted; fault-to-recovery loop verified on a real cluster
- Scope: one CrashLoopBackOff / early Liveness Probe diagnosis, read-only Tools

| 验收项 | 自动验证 | 结果 |
|---|---|---|
| FastAPI 可启动，POST/GET 严格 DTO | `tests/integration/api/test_diagnosis_api.py` | Pass |
| Router 结构化输出稳定 | `tests/unit/routing/`、`tests/integration/runtime/test_offline_e2e.py` | Pass |
| Planner 选择正确只读 Tool 并经过 Validator | `tests/unit/planning/`、`tests/integration/runtime/test_offline_e2e.py` | Pass |
| 五个 Kubernetes Tool 的 Schema、错误及清洗 | `tests/unit/tools/kubernetes/`、`tests/integration/tools/` | Pass (Fake/Replay) |
| CrashLoopBackOff Offline E2E Ground Truth | `tests/integration/runtime/test_offline_e2e.py`、API Replay 测试 | Pass |
| LLM/Tool Calls 按 Run 与 Trace 审计 | `tests/integration/runtime/test_offline_e2e.py`、API 数据库断言 | Pass |
| 四类 required Evidence、绑定 Claim/Verification | Runtime E2E、API GET | Pass |
| Root Cause、Evidence、Recommendation | API GET + 确定性 Verifier | Pass |
| 任务幂等、容量、关闭和稳定错误 | `tests/unit/api/`、`tests/integration/api/` | Pass |
| 真实集群五个 Tool | `tests/integration/api/test_live_smoke.py` | Pass (kind，真实只读 ServiceAccount) |
| Live Runtime 端到端诊断 | `tests/integration/api/test_live_smoke.py` | Pass（操作者报告 1 passed，43.14s） |
| 修复后 Ready 与零重启 | `manifests/fixed-deployment.json` + `kubectl rollout status` | Pass（Running、Ready、restartCount 0） |

验证记录：

```text
python -m pytest -q
285 passed, 1 skipped

python -m mypy src tests
Success: no issues found in 122 source files

python -m pytest tests/integration/api/test_live_smoke.py -m live -q
1 passed in 43.14s
```

## 首次 Live 失败记录（历史）

首次人工验证：**2026-09-23**。

环境：kind `opspilot`（kubectl v1.37.0、kind v0.33.0、Docker 29.8.0），`opspilot-fixtures` namespace 内故障 Deployment 持续 CrashLoop，只读身份仅授予 `pods get/list`、`pods/log get`、`events list`、`deployments.apps get`。模型端点支持 OpenAI Responses API 与结构化输出。

结果：

- 五个 Risk 0 只读 Tool 对真实集群调用全部成功。
- Router 真实模型调用成功并写入审计（含 provider 返回的实际模型版本）。
- Planner 调用返回 HTTP 400，原始错误为
  `Invalid schema for response_format 'ExecutionPlanV1': 'required' is required to be supplied and to be an array including every key in properties. Extra required key 'arguments' supplied.`
- 根因：`responses.parse` 恒定发送 `strict: true`，而 `ExecutableStepV1.arguments: dict[str, JsonValue]` 在 strict 规则下不可表达（自由对象无法 `additionalProperties: false`；`JsonValue` 塌缩为空 schema 缺少 `type`；嵌套 `$defs`/`$ref` 被上游错误合并 `required`）。扁平全类型化的 Router Schema 正常，缺陷限于计划 Schema。
- 六个可用模型与两个不同凭据复现同一拒绝，与 provider 无关，故 OpenAI 官方 API 高概率同样拒绝（无官方凭据，未直接验证）。
- Run 以 FAILED 结束，API 对外只返回固定文案，未泄漏 provider 文本、Prompt 或凭据；审计表完整保存失败尝试与错误码。

首次结论：V1 离线验收保持通过；Live 端到端诊断**未通过**，被上述 Schema 缺陷阻断，已登记到 `docs/STATUS.md` 并作为发布阻断项。修复后的真实复测通过前不得宣称 Live 生产可用。V1 API 无认证，不得公开部署。进程内队列无崩溃恢复，多副本/滚动发布目标解析、长期原始结果保留等仍属后续阶段。

## 修复与复测状态

首次 Live 记录保留如上。V1-008 已按 [ADR 0004](../adr/0004-strict-model-wire-envelope.md)
在 OpenAI Adapter 增加扁平 `payload_json` wire Schema，解码后仍严格验证
`ExecutionPlanV1` / `DiagnosisDraftV1`，不绕过 Plan Validator 或 Verifier。
Live Planner/Diagnosis Prompt 使用 v2，历史 v1 未改写。本地 SDK 生成的
Schema 只有一个必填字符串字段，无动态对象或 `$defs`；模拟 Live API
从 Router 到最终 Result 已通过。

后续真实运行中，Planner v2 字段名提示、模型调用超时配置和 fixture
日志格式/提前终止已修正。修复 Reader 前一次 Run 的 Router、Planner、Diagnosis
三次真实模型调用及四次 Runtime 只读 Tool 调用全部成功，最终状态却为
`PARTIAL`：Evidence 包含 status×1、events×3、deployment×1、logs×0；
`verification.missing_evidence` 只有 `startup_duration`，无 contradictions。
这次结果证明首次 Schema 阻断已经跨过，但尚不能宣称端到端 Live 成功。

其后的阻断是 Kubernetes SDK 36.0.3 默认日志响应的内容为 bytes 的
`repr` 字符串（行首 `b'`、换行为字面量 `\\n`），导致 Previous Logs
Extractor 的多行正则无法产生 Evidence。Reader 已改用
`_preload_content=False`，直接限量读取原始响应字节并按 UTF-8 解码；
边界测试覆盖换行、限量读取和连接释放，全量测试与 strict mypy 通过。

## 最新 Live 验收

2026-09-23，在已配置的真实 kind 环境运行
`pytest -m live`，结果 **1 passed（43.14s）**。测试依次调用五个
Risk 0 只读 Tool，再经 POST/GET 执行完整 Live Runtime；最终状态
`COMPLETED`，Evidence 覆盖 `kubernetes_status`、
`kubernetes_events`、`kubernetes_logs`、
`kubernetes_deployment`，`verification.supported=true`。
这说明 E1 strict Schema 与 E5 bytes repr 两处阻断已通过真实边界复测。

## 修复侧核验与收尾

同一环境应用 `manifests/fixed-deployment.json` 后：

```text
kubectl -n opspilot-fixtures rollout status deployment/slow-start-api --timeout=150s
deployment "slow-start-api" successfully rolled out

kubectl -n opspilot-fixtures get pods
slow-start-api-74574b59d7-7k6cs   1/1   Running   0   2m29s
```

- `phase: Running`，`Ready condition: True`，`restartCount: 0`，`lastState: {}`（容器从未被终止）；90 秒后复检仍为 0。
- 新 Pod 只有一条 `Startup probe failed` 事件（10:43:27），且**无任何 `Killing` 事件**。这条探测失败正是修复生效的证据：应用第 40 秒才监听，`periodSeconds 5 × failureThreshold 10 = 50` 秒预算容忍了它；10:43:28 应用 ready 后探测转成功，Liveness 此时才被放行。其余 Unhealthy/Killing/BackOff 事件时间戳均落在 10:01–10:38，属于 CrashLoop 期间的旧 Pod。
- 应用日志：`10:42:48 application boot started` → `10:43:28 application ready`（恰好 40 秒），随后 `/healthz` 返回 200。
- 与 `fixtures/cases/crashloop-liveness-v1/case.json` 的恢复 Ground Truth 一致：`phase Running`、`ready true`、`max_restart_count 0`。
- 清理：`kubectl delete -f manifests/namespace.json` 后 namespace 消失，只读 ServiceAccount、Role、RoleBinding 与 token Secret 随之删除，残留资源检查为空。

## 结论

V1 单故障闭环（部署故障 → 只读诊断 → 应用修复 → 核验 Ready/零重启 → 清理）已在真实 Kubernetes 集群完整通过，离线自动验收与 strict mypy 同时保持通过。首次 Live 的失败记录保留在上文，其两处阻断（strict Schema 不可表达、SDK 日志 bytes repr）均已修复并有边界测试覆盖。

V1-008 验收完成。V1 API 无认证，不得公开部署；进程内队列无崩溃恢复，多副本/滚动发布目标解析、长期原始结果保留等仍属后续阶段。
