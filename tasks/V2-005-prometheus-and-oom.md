# V2-005: Prometheus 指标边界与 OOMKilled 纵向切片

- Status: Done
- Phase: V2
- Depends on: V2-003, V2-004

## 目标

提供四个受限 Prometheus 只读 Tool，并让 OOMKilled 成为第一个真实
观察驱动的 V2 故障 Case：看到 OOM 终止后才读取内存限额与峰值，
而不是继续走 Probe 固定流程。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/adr/0005-bounded-observation-planning-v2.md`
- `docs/architecture/evidence-model.md`
- `docs/architecture/security-boundary.md`

## 架构与实现

- 新增窄 `PrometheusReader` Adapter，固定服务端配置与 TLS/认证；
  不允许模型提供 URL、PromQL 或任意 label matcher。
- 注册 `prometheus.query_cpu`、`prometheus.query_memory`、
  `prometheus.query_latency`、`prometheus.query_error_rate`。
  输入为校验后的 namespace/workload、枚举指标、受限时间窗、
  step 与样本上限；输出带单位、采样时间、来源与缺样本状态。
  每个 Tool 声明 Risk 0、超时、幂等分类重试与输出限长。
- Adapter 区分 HTTP/认证/超时/无 series/陈旧 series/非法数字；
  统一 Error Taxonomy，不把查询或敏感标签暴露到审计/模型。
- OOM Case 的确定性 Evidence 包含 OOMKilled termination、容器
  memory limit、临近终止内存曲线峰值。Verifier 只在数据足够时
  判断限额与峰值关系；OOM reason 不自动等于“限额过低”。
- Replay 同时覆盖有峰值、无指标、指标与终止时间不重合以及
  另一分支（probe 失败）的路径差异。

## 测试

- 四个模板查询输入、单位、窗口、上限、空/NaN/陈旧序列及权限
  失败的 Adapter/Tool 边界测试。
- OOM 完整 Case -> supported；缺峰值 -> Partial；无 OOM reason
  或峰值与时间不符 -> 不支持该精确根因。
- 同一初始 Pod 重启症状在 OOM Evidence 与 liveness Evidence 下
  选择不同第二轮 Tool；调用/成本预算和审计均保持有界。
- 可选真实 Prometheus smoke 只验证隔离测试指标，不使用生产查询。

## 不做

- 不允许自由 PromQL、跨 namespace 查询或写入/修改告警规则。
- 不在指标缺失时由模型补全内存峰值。

## 验收条件

- [x] 四个指标 Tool 经 Registry/Policy 可用且具稳定形态测试。
- [x] OOMKilled Case 有正例、反证与缺指标 Partial。
- [x] 动态分支、Replay、预算与审计测试通过；可选只读 Live 入口已加入，真实 Prometheus 尚未运行。
- [x] 默认 pytest、strict mypy 与 STATUS 更新通过。

## 完成记录

- `PrometheusHttpReader` 使用显式 HTTPS origin、TLS 验证、受限响应体与去敏错误；四个固定模板 Tool 仅接受受限 namespace/workload、窗口、step 和可选精确 Pod/container，不接受模型提供的 PromQL、URL 或标签匹配式。
- 空/陈旧序列保留 missing/stale 状态，NaN、非法形态、跨窗口样本与超量结果拒绝；独立 V2 Evidence 提取记录 OOM 终止、同容器内存限额及指标样本，V1 提取器与五 Tool 行为不变。
- `OomKilledVerifierV2` 只在单个同容器 OOMKilled、及时限额和终止前 120 秒内的精确指标峰值齐全且峰值接近限额时支持限额相关诊断。缺峰值、错时间、低峰值或无 OOM reason 保持 Partial。
- 新 `oom-limit-v2.json` 的峰值存在/缺失两分支经 SQLite Runtime/Replay 完整走通；沿用 V2-003 的同症状 OOM/Probe 动态第二轮分支与预算回归。2026-09-23 默认全量 361 passed、3 skipped（均为 opt-in Live），strict mypy 154 文件通过。真实 Prometheus smoke 未执行。
