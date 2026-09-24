# V2-004: Kubernetes 拓扑、资源与多 Pod 只读 Tool

- Status: Done
- Phase: V2
- Depends on: V2-002

## 目标

提供 Service、EndpointSlice、Ingress 和资源使用的结构化读取，
并把 V1 的单候选 Pod 解析扩展为可审计的多副本/滚动发布快照。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/security-boundary.md`
- `src/opspilot/integrations/kubernetes/`
- `src/opspilot/tools/kubernetes/`

## 架构与实现

- 在窄 `KubernetesReaderV2` 边界新增所需 SDK 读方法；
  `k8s.get_service`、`k8s.get_endpoints`（DiscoveryV1
  EndpointSlice）、`k8s.get_ingress`、`k8s.get_resource_usage`
  均有严格输入/输出、Risk 0、超时和分类重试声明。
- 只允许已校验 namespace/名称和从已读对象导出的 selector。
  Service 端口、targetPort、EndpointSlice ready/serving 条件、
  Ingress route/backend、UID/resourceVersion 和采样时间结构化输出。
  多 Pod 结果稳定排序、限量、标注 rollout generation；歧义不选首个。
- 资源使用来自可配置的只读 Metrics API；不可用/陈旧/缺样本返回
  稳定错误或缺失信号，不伪造零值。不得读取 Secret 内容、执行
  `kubectl` 或输出原始环境变量。
- 为每个输出定义确定性 Evidence Extractor；敏感注解、主机名、
  标签高基数字段和异常正文限长/清洗。
- ServiceAccount 权限只新增必要 get/list；Live 凭据与 V1 读权限
  分开准备，缺权限不降级到高权限身份。

## 测试

- 空 EndpointSlice、mixed ready、端口错配、多副本、滚动发布、
  selector 不匹配、权限拒绝和 Metrics API 不可用。
- SDK response shape/bytes、分页与截断边界，Tool Schema/Policy/
  timeout/错误映射及 Evidence provenance。
- 旧 V1 单副本目标解析与五 Tool 行为不变。

## 不做

- 不进行集群写入、Secret get、exec、portforward 或任意 selector 查询。
- 不在此任务中推断 503/Readiness 根因。

## 验收条件

- [x] 四个 Tool 经 Registry 可用，严格只读且按边界限量。
- [x] 多 Pod/rollout 歧义有确定性结果，不任意挑选。
- [x] Fake 与 SDK 边界测试通过；可选只读 Live 测试已加入并默认跳过，真实集群尚未执行。
- [x] 默认 pytest、strict mypy 与 STATUS 更新通过。

## 完成记录

- V2 Reader 与 Registry 增加 Service、EndpointSlice、Ingress、PodMetrics 四个 Risk 0 Tool；保留 V1 五 Tool 行为。Service/Endpoint/Ingress/Metrics 归一化输出与 Evidence Extractor 独立于原始 SDK 对象。
- Deployment selector 只能从已读取对象导出，响应侧再次核对 Pod 标签；多副本稳定排序，缺失模板 hash、混合 rollout 或截断均标注歧义。Metrics 对缺样本、过期/未来采样、分页截断与超过 100 条容器样本显式处理。
- 规划器只接收白名单数值、布尔和格式受限的结构化事实；跨轮 Evidence 限为 100 条。独立 V2 ServiceAccount 的最小权限和 Live opt-in 前置条件见 `docs/architecture/kubernetes-tools-v2.md`。
- 2026-09-23 默认全量 `352 passed, 2 skipped`（V1/V2 两个 opt-in Live）；strict mypy 147 文件通过。真实 V2 集群 smoke 未执行，不作为已验证的部署声明。
