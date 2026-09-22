# Kubernetes Tools V1

## 目标

V1 公开且只公开五个 Risk 0 Tool：

```text
k8s.get_pod_status
k8s.get_pod_events
k8s.get_pod_logs
k8s.get_previous_logs
k8s.get_deployment
```

所有实现使用官方 Kubernetes Python SDK，经 `KubernetesReader` 协议隔离。Handler 不读取全局 kubeconfig、不执行 shell，也不返回 SDK 对象。

## Client Boundary

`KubernetesReader` 是适配器边界，包装 `CoreV1Api` 和 `AppsV1Api` 所需的最小读取方法：

```text
read_pod
list_pods
list_events
read_pod_log
read_deployment
```

生产 Adapter 负责 SDK 调用和异常翻译；单元测试使用 Fake Reader。Tool Handler 只依赖该协议，因此无需 mock SDK 内部实现。

V1-001 的生产实现使用官方 Kubernetes Python SDK `>=36.0.3,<37`，并遵守以下边界：

- 每个 Reader 实例使用独立 `Configuration`，不修改 SDK 全局默认配置。
- 生产默认仅加载 in-cluster config；本地开发必须同时显式提供 kubeconfig 路径和 context，禁止自动扫描默认 kubeconfig。
- 所有 SDK 调用显式传入 namespace 和 request timeout；Events 还必须传入数量上限。
- SDK Model 在 Adapter 内通过 `sanitize_for_serialization` 转换为 JSON-only 对象，后续层不接触 SDK 类型。
- Adapter 仅返回稳定、去敏后的内部错误，不透传 API 响应正文、原始异常文本或堆栈。

## 目标解析

Pod 级 Tool 接受以下二选一目标：

- 精确 `pod_name`。
- `workload_name`，V1 仅解释为 Deployment。

workload 解析流程：

1. 读取同 namespace 的 Deployment。
2. 从 `spec.selector.matchLabels` 构造精确 label selector。
3. 列出匹配 Pod，排除带 deletion timestamp 的对象。
4. V1 只接受恰好一个候选 Pod。
5. 零候选返回非重试错误；多候选返回目标歧义错误，不自动挑选。

这条规则适配 V1 单副本 Case。滚动发布、多副本选择和 Service 到 Pod 的解析推迟到 V2。

## 公共输入约束

- `namespace`、Pod、Deployment 和 container 名称使用 Kubernetes DNS/资源名约束。
- Pod Tool 必须且只能提供 `pod_name` 或 `workload_name`。
- 日志请求默认 `tail_lines=200`，最大 1000；默认最大 64 KiB。
- Events 最多返回 100 条，按 `last_timestamp`、`name` 稳定排序。
- Tool timeout 不超过 10 秒；SDK request timeout 必须小于 Registry timeout。
- 所有输入拒绝路径片段、空白控制字符、通配符和任意 label selector。

Planner 不能提供自由 label selector；selector 只能由已读取 Deployment 生成。

## V1 Tool 实现

五个 Handler 位于 `opspilot.tools.kubernetes`，只依赖 `KubernetesReader` 和 `PodTargetResolver`。`build_kubernetes_registry` 注册且只注册本文定义的五个 Tool。

- Pod Status 复用 Resolver 已读取的 Pod，不为规范化状态重复请求 API。
- Events 使用 Pod UID 与名称组成 field selector，最多请求并返回 100 条；结果按最新时间优先、事件名称升序稳定排列，message 最长 2048 字符。
- Current/Previous Logs 共用 UTF-8 字节截断逻辑，输出不超过 64 KiB；多容器目标未指定 container 时明确失败。
- Deployment selector、Probe、resources 和 environment 被转换为封闭 Schema；不返回未声明的 SDK 字段。
- 敏感名称的 literal environment value 不进入输出；SecretKeyRef 仅保留引用元数据并固定标记为 redacted。

## Tool Schema

### k8s.get_pod_status

输入：PodTarget。

输出：

```text
namespace
pod_name
phase
conditions[]
containers[]
  name
  ready
  restart_count
  state
  reason
  last_exit_code
  last_reason
  last_started_at
  last_finished_at
```

Evidence：每个异常 container 可产生状态 Evidence；首个 Case 至少记录 CrashLoopBackOff、restart count 和 last termination。

### k8s.get_pod_events

输入：PodTarget，解析后使用 Pod UID/名称过滤。

输出：

```text
namespace
pod_name
events[]
  type
  reason
  message
  count
  first_timestamp
  last_timestamp
```

Evidence：Warning 事件和 Killing/BackOff 等诊断事件。消息保持数据属性，进入模型前限长。

### k8s.get_pod_logs

输入：PodTarget、可选 container、tail lines、since seconds。

输出：

```text
namespace
pod_name
container
previous = false
content
truncated
byte_count
```

多容器 Pod 未指定 container 时必须返回歧义错误，不选择第一个容器。

### k8s.get_previous_logs

与当前日志使用同一输入输出模型，但 SDK 调用设置 `previous=true`，输出固定 `previous=true`。无 previous instance 时返回非成功 ToolResponse，不伪造空日志。

Evidence：首个 Case 从启动日志抽取声明的启动耗时和被终止时间。抽取规则由 V1-005 的 Evidence Extractor 实现，不放入 SDK Adapter。

### k8s.get_deployment

输入：namespace、deployment name。

输出：

```text
namespace
name
generation
replicas
ready_replicas
unavailable_replicas
selector
containers[]
  name
  image
  liveness_probe
  readiness_probe
  startup_probe
  resources
  environment[]
```

Probe 使用结构化 HTTP/TCP/exec 类型和时间参数。SecretKeyRef 的值永不读取；敏感名称的 literal env value 必须标记 redacted。首个 Case 的 `STARTUP_DELAY_SECONDS` 可作为非敏感运行参数保留。

Evidence：Probe 时间窗口、Startup Probe 是否存在、镜像和资源约束等事实。

## 标准错误映射

| Kubernetes/SDK 情况 | ErrorCode | 重试 |
|---|---|---|
| 输入 Schema、404、零候选、多候选、container 歧义 | `INVALID_ARGUMENT` | 否 |
| 401/403 | `PERMISSION_DENIED` | 否 |
| Registry 或 SDK timeout | `TOOL_TIMEOUT` | 按 Taxonomy |
| 429、网络中断、暂时性 5xx | `EXTERNAL_SERVICE_ERROR` | 按 Taxonomy |
| 无法归类的 SDK 异常 | `TOOL_EXECUTION_FAILED` | 最多 1 次 |
| Handler 输出不符合 Schema | `TOOL_OUTPUT_INVALID` | 否 |

错误消息只包含资源类型、经校验的名称和稳定摘要，不包含响应正文、凭据或堆栈。

这些错误由 `opspilot.integrations.kubernetes` 边界统一承载 `ErrorCode`。Tool Handler 在 V1-002 只负责把该稳定错误投影为 `ToolResponse`，不重新解释 SDK 异常。

Kubernetes API 的 400 响应映射为 `INVALID_ARGUMENT`，用于无 previous container instance 等不可重试请求失败；响应正文仍不透传。

## RBAC

V1 Service Account 最小权限：

```text
pods: get, list
pods/log: get
events: list
deployments.apps: get
```

不授予 create、update、patch、delete、exec、attach、portforward、secrets 或 configmaps 权限。V1 Tool Definition 全部声明 `READ_ONLY`。

## 测试要求

- 每个 Tool 对正常、空值、多容器、404、403、timeout 和非法输出至少覆盖相关路径。
- Fake Reader 验证 Handler 只调用声明的方法和限定 namespace。
- 日志截断、事件排序、Secret/env 清洗有独立测试。
- 资源解析覆盖精确 Pod、单 Pod workload、零 Pod 和多 Pod。
- Registry 集成测试验证五个名称全部注册、均为 Risk 0、Descriptor 可供 Planner 使用。
- Live smoke test 使用 `opspilot-fixtures` namespace，可通过标记在无集群环境跳过。
