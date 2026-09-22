# V1-001: Kubernetes Client Boundary 与目标解析

- Status: Ready
- Phase: V1
- Depends on: V0-001, V0-002, V0-003

## 目标

建立 Kubernetes Python SDK 与 OpsPilot Tool 之间的最小只读边界，定义公共输入输出模型、SDK 异常翻译和 Deployment 到单 Pod 的确定性解析，为五个 Tool 提供可测试基础。

## 上下文

- `docs/architecture/v0-contract-baseline.md`
- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/security-boundary.md`
- ADR 0001、ADR 0003

## 架构位置

```text
Kubernetes Tool Handler
  -> KubernetesReader Protocol
       -> KubernetesSdkReader
            -> CoreV1Api / AppsV1Api
```

Tool Handler 只能依赖 Protocol，不能直接依赖 SDK Client。LLM、Planner 和 Executor 不得接触 Reader 或 SDK 对象。

## 范围

1. 在 `pyproject.toml` 增加受限版本的 Kubernetes Python SDK。
2. 建立 `opspilot.integrations.kubernetes` 模块：
   - `KubernetesReader` Protocol。
   - `KubernetesSdkReader` 生产适配器。
   - SDK DTO 到纯 Python/Pydantic 数据的转换边界。
   - 可注入 request timeout、clock 和配置。
3. 定义公共严格 Schema：
   - Namespace、Pod、Deployment、container 名称。
   - 精确 Pod 或 workload 二选一的 `PodTarget`。
   - 日志查询边界：tail lines、since seconds、最大字节数。
4. 实现 `PodTargetResolver`：
   - pod name 直接读取。
   - workload name 读取 Deployment selector 后 list Pods。
   - 排除 terminating Pod。
   - 零候选和多候选均明确失败。
5. 定义内部 Kubernetes 错误：
   - Not Found / Ambiguous Target。
   - Permission。
   - Timeout。
   - Transient / Unexpected。
6. 配置加载：
   - 生产支持 in-cluster config。
   - 开发只在显式配置时读取指定 kube context。
   - 禁止自动扫描或把 kubeconfig 内容写入 Trace。

## 关键决策

- V1 workload 只解释为 Deployment，且只支持单副本确定性解析。
- Planner 不可提供自由 label selector；selector 只能来自已读取 Deployment。
- Reader 不返回原始 SDK Model，防止后续层依赖 SDK 内部字段。
- 404 在 Tool 层映射为 `INVALID_ARGUMENT`；403 映射 `PERMISSION_DENIED`；timeout 和暂时性错误按统一 Taxonomy 处理。

## 预期目录

```text
src/opspilot/integrations/kubernetes/
  __init__.py
  client.py
  errors.py
  models.py
  targeting.py
```

目录名可按代码库模式微调，但职责不得合并进 Tool Registry。

## 测试策略

- 使用 Fake Reader/API 验证所有 SDK 路径，不要求 CI 存在集群。
- 精确 Pod、单 Pod workload、零 Pod、多 Pod、terminating Pod。
- 非法 namespace/name、同时提供 pod/workload、二者都缺失。
- 404、403、timeout、429/5xx 和未知异常的稳定翻译。
- 配置测试确保默认路径不暴露 kubeconfig 或凭据。
- mypy strict 验证 Protocol 与 Adapter。

## 不做

- 不注册 Kubernetes Tool。
- 不生成 Evidence。
- 不支持 Service、StatefulSet、DaemonSet、多副本或滚动发布选择。
- 不实现 watch、cache、写操作、exec 或任意 shell。

## 验收条件

- [ ] Kubernetes SDK 被隔离在 Adapter 内，Tool/Runtime 可使用 Fake Reader。
- [ ] PodTarget 只能表示一个合法、受限目标。
- [ ] Deployment 到单 Pod 的解析确定且对零/多候选失败。
- [ ] SDK 错误不泄漏响应正文、凭据或堆栈。
- [ ] 所有 API 调用带 namespace 和超时。
- [ ] 单元测试与 strict mypy 通过。
- [ ] 更新 Kubernetes Tool 架构文档和项目状态。
