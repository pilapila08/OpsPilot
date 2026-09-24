# V2-011: Service 503 拓扑诊断

- Status: Planned
- Phase: V2
- Depends on: V2-003, V2-004, V2-005

## 目标

跨 Ingress、Service、EndpointSlice 与 Pod readiness 解释 503，
区分无 ready 后端、selector/port 错配和仍需调查的上游错误。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/evidence-model.md`

## 架构与实现

- Case v2 声明 Ingress route -> Service -> EndpointSlice -> Pod UID
  的 scoped 拓扑和时间戳；不从用户自由文本构造 selector。
- Planner 先取 503/error-rate 与 route，再依观察选择 Service/
  EndpointSlice、Pod readiness 或端口配置；已有 ready 后端时
  不强行走“无 Endpoint”分支。
- Evidence Extractor 生成 route/backend、selector、port mapping、
  ready/serving 地址数、Pod readiness 和 503 时间窗信号；结果
  限量并标注资源版本，防止滚动发布跨快照误关联。
- Verifier 对“无 ready Endpoint”“selector 不匹配”“targetPort
  错配”分别设置必需 Evidence；若拓扑健康、HTTP 503 来自应用
  或时间窗不重合，拒绝这三个具体根因并给 Partial/其它线索。
- 多 Service/多 Pod 结果保持稳定排序，歧义明确输出，不任意
  挑选首个对象。

## 测试

- 空 EndpointSlice、全 unready、mixed ready、selector/port
  错配、Ingress backend 不匹配、应用自己返回 503、rollout
  期间旧 Endpoint 与新 Pod 并存。
- 反证、缺指标/缺权限/缺拓扑和跨 namespace 请求被安全处理。
- Replay 决策路径不同，Tool/Evidence/Result 可按 Trace 重建。

## 不做

- 不修改 Service/Ingress/Deployment，不做主动流量探测或请求注入。
- 不把所有 503 归因于 Kubernetes Endpoint。

## 验收条件

- [ ] 503 子因有独立 Evidence 门槛和反证。
- [ ] 多副本与 rollout 快照不制造错误拓扑结论。
- [ ] 只读 Replay 和可选隔离 Live smoke 通过。
- [ ] 默认 pytest、strict mypy 与 STATUS 更新通过。
