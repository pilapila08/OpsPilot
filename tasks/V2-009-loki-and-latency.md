# V2-009: Loki 日志边界与延迟升高诊断

- Status: Planned
- Phase: V2
- Depends on: V2-003, V2-005

## 目标

增加一个受限 Loki 只读 Tool，并在 p95/p99 确实升高时，用指标、
资源和有限日志信号诊断服务延迟，而不把相关性直接写成根因。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/evidence-model.md`
- `docs/architecture/security-boundary.md`
- `docs/roadmap/v1-acceptance.md`（真实边界形态经验）

## 架构与实现

- `LokiReader` 使用固定端点/租户配置；`loki.query_logs`
  输入只允许已验证 namespace/workload/container、受限相对时间窗、
  行数和字节上限，服务端组装固定查询模板，不接受模型 LogQL。
  Tool 定义 Risk 0、严格输出模型、超时和分类重试。
- Adapter 解码真实 Loki JSON/流形态，处理空结果、过期时间窗、
  分页、429/5xx、权限拒绝、非法 UTF-8 和高基数标签；日志正文
  不进诊断摘要或系统指令，敏感值清洗后只产出确定性信号。
- Latency Case 需要 p95/p99 的基线与当前窗口、单位、样本覆盖率，
  再按观察追加 CPU、memory、error rate 和日志/依赖信号。
  缺指标或仅有资源相关性时 Verifier 输出 Partial。
- Verifier 对采样窗口不重合、无显著回归、日志早于问题或
  指标陈旧设置反证；具体根因须有直接机制 Evidence。

## 测试

- Loki 真实形态样本、bytes/UTF-8、空/错时间窗、限长、注入文本、
  凭据清洗和错误 Taxonomy。
- p95/p99 升高且有直接资源/错误信号 -> supported；
  仅延迟曲线或相关 CPU 上升 -> Partial；无回归 -> 不支持。
- Replay 展示先查延迟再按观察选择下一 Tool；Trace 证明不超预算。
- 可选 Live smoke 使用隔离日志源和只读凭据。

## 不做

- 不开放任意 LogQL，不允许跨租户/跨 namespace 读日志。
- 不因一行错误日志自动推断全服务瓶颈。

## 验收条件

- [ ] Loki Tool 在 Registry 中只读、受限且真实形态边界测试通过。
- [ ] Latency Case 有正例、反证和 Partial 分支。
- [ ] 日志/指标不可信内容不提升为控制指令或未验证根因。
- [ ] 默认 pytest、strict mypy 与 STATUS 更新通过。
