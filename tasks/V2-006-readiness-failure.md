# V2-006: Readiness Probe Failure 诊断

- Status: Ready
- Phase: V2
- Depends on: V2-003, V2-004

## 目标

对 Readiness Probe Failure 给出有证据支撑的原因，能区分应用
未监听、探针端口/路径配置错误和下游依赖不可用，并解释 Service
为何没有 ready Endpoint。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/diagnosis-v1.md`

## 架构与实现

- V2 Case 固定 Pod/Deployment/Service 关联及 fault/recovery 快照，
  记录 Ready condition、readiness Events、probe 配置、监听端口
  或依赖信号、EndpointSlice ready 状态。
- Planner 先取 Pod readiness 与 Events，再按观察选择 Deployment
  probe/端口、Service/EndpointSlice 或受限日志；不必无条件读全部。
- Evidence Extractor 只从结构化 condition、port、endpoint 条件和
  经校验的日志模式生成信号；Event 自由文本不直接成为根因。
- Verifier 针对具体子因设置必需 Evidence 与反证：Pod 已 Ready、
  Endpoint ready 或端口一致时，不得仍断言对应配置错误。缺少
  依赖状态时只报告 readiness 失败事实或 Partial。
- Recommendation 只给操作者检查/修复建议，Agent 不改探针。

## 测试

- 未监听、probe 端口错配、依赖不可用、Endpoint 排除四条
  Replay 路径及至少一条恢复快照。
- Ready=True、正常 Endpoint、缺失日志/依赖、滚动发布多 Pod
  和矛盾证据的否定/Partial 测试。
- Tool 调用与 Evidence ID 可按 Trace 追溯，V1 CrashLoop 回归不变。

## 不做

- 不自动修复 Deployment，不把单个不可用 Pod 推断为全 Service 故障。
- 不读取应用 Secret 或依赖服务凭据。

## 验收条件

- [ ] Readiness Case 的支持/反证/缺证据分支均可回放。
- [ ] 动态选择 Tool 且多 Pod 情况不丢失目标身份。
- [ ] Verifier 的精确子因只引用当前 Trace Evidence。
- [ ] 默认 pytest、strict mypy 与 STATUS 更新通过。

## 当前进展（未验收）

- 2026-09-23 已在 `V2EvidenceExtractorRegistry` 中增加 Pod `Ready` condition、经分类的 readiness failure Event 和 Deployment readiness probe 三类结构化 Evidence；事件原文不进入 Evidence content，V1 提取器保持不变。
- 单元测试覆盖 V2 信号及 V1 不受影响；当前默认全量 363 passed、3 skipped（均为 opt-in Live），strict mypy 155 文件通过。
- 尚缺 EndpointSlice 到具体 Pod 的关联 Evidence、端口/路径及依赖子因规则、确定性 Verifier、四条 Replay 分支与恢复快照；以上验收条件仍未勾选。
- 2026-09-24 按用户优先级暂缓，保留已实现的 V2 Evidence 与未勾选验收项；后续从此处续做。
