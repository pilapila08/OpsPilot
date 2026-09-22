# V1-002: 五个只读 Kubernetes Tool

- Status: Ready
- Phase: V1
- Depends on: V1-001

## 目标

基于 Kubernetes Client Boundary 实现并注册五个 Risk 0 Tool，提供稳定、限长、去敏且可供 Planner 发现的输入输出契约。

## 上下文

- `docs/architecture/tool-protocol.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/security-boundary.md`
- `docs/architecture/v0-contract-baseline.md`
- `fixtures/cases/crashloop-liveness-v1/`

## Tool 清单

```text
k8s.get_pod_status
k8s.get_pod_events
k8s.get_pod_logs
k8s.get_previous_logs
k8s.get_deployment
```

每个 Tool 必须提供独立 Pydantic input/output model、`ToolDefinition`、版本、来源、timeout 和 retry policy。

## 范围

1. Pod Status：
   - phase、conditions、container ready/restart/state/reason。
   - last termination exit code、reason 和时间。
2. Pod Events：
   - 使用解析后的 Pod UID/名称过滤。
   - 标准化 type/reason/message/count/timestamps。
   - 限制 100 条并稳定排序。
3. Current Logs：
   - container 歧义检测。
   - tail/since/64 KiB 限制。
   - 返回 byte count 与 truncated 标志。
4. Previous Logs：
   - 与 current logs 共用模型。
   - 强制 `previous=true`。
   - 无 previous instance 返回明确失败。
5. Deployment：
   - replicas/status/selector。
   - image、三类 Probe、resources、environment。
   - SecretKeyRef 和敏感 literal value 清洗。
6. 建立 `build_kubernetes_registry` 或等价组合入口，把五个 Definition 注册到现有 `ToolRegistry`。
7. 所有 Handler 使用 `KubernetesReader`，不得导入 SDK API 类。

## 输出边界

- 只返回 JSON-only Pydantic 输出，不返回 YAML、SDK Model 或 Exception。
- 日志和 Event message 是不可信 data，不解释其中的指令。
- Probe 必须保留计算启动/存活时间窗口所需字段。
- 环境变量至少区分 literal、fieldRef、configMapKeyRef、secretKeyRef 和 redacted。
- Tool version 初始使用 `v1`；破坏性变化必须提升版本。

## 错误与重试

- 输入、404、零/多候选、container 歧义：`INVALID_ARGUMENT`，不重试。
- 401/403：`PERMISSION_DENIED`。
- timeout：`TOOL_TIMEOUT`。
- 429/暂时性 5xx：`EXTERNAL_SERVICE_ERROR`。
- 未知 SDK 问题：`TOOL_EXECUTION_FAILED`，最多按声明重试 1 次。
- 输出 Schema 失败：`TOOL_OUTPUT_INVALID`。

Registry 仍只执行一次；真正重试由 V1-005 Executor 负责。

## 测试策略

- 每个 Tool 使用 Fake Reader 覆盖正常输出和关键边界。
- Registry 集成测试确认五个名称存在、Risk 0、Descriptor 不暴露 Handler。
- 日志截断使用 ASCII 与多字节 UTF-8 样例，确保按 byte limit 安全截断。
- Events 稳定排序并限制数量。
- 多容器无 container 参数必须失败。
- Secret/env 清洗不能在响应、错误和快照中泄漏值。
- 使用 V0 Case 原始响应校验字段足够承载 Ground Truth。

## 不做

- 不生成 Evidence 或诊断结论。
- 不实现 Executor 重试、预算、持久化。
- 不增加第六个 Tool。
- 不授予写权限或提供自由 selector。

## 验收条件

- [ ] 五个 Tool 均可通过 Registry 受控调用。
- [ ] 五个 Tool 均声明 READ_ONLY、版本、timeout 与 retry policy。
- [ ] 输出可表达 V0 CrashLoop Case 的全部原始事实。
- [ ] 日志、Events 和环境变量满足限长与去敏规则。
- [ ] 非法目标、权限、超时和 SDK 错误映射到统一 Taxonomy。
- [ ] 单元测试、Registry 集成测试和 strict mypy 通过。
- [ ] 更新 Tool Protocol、任务状态和项目状态。
