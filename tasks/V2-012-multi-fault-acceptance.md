# V2-012: 八类故障、多数据源与动态规划验收

- Status: Planned
- Phase: V2
- Depends on: V2-005, V2-006, V2-007, V2-008, V2-009, V2-010, V2-011

## 目标

按 V2 架构矩阵做阶段级验收，证明八类故障、10+ 可用只读 Tool、
多数据源和观察驱动路径在同一有界 Runtime 中运行，留下可信
验收报告与后续风险清单。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/roadmap/README.md`
- `docs/roadmap/v1-acceptance.md`
- `tasks/V2-002-case-replay-and-intent-v2.md` 至
  `tasks/V2-011-service-503.md`

## 验收设计

- 每类 Case 至少有 Ground Truth 正例、反证、缺数据 Partial、
  恢复快照或明确不适用说明；八类都经相同 Router/Planner/
  Validator/Registry/Evidence/Verifier/Result 链路。
- 至少十个 Tool 不仅注册，还能经 Schema/Policy、Adapter、
  Replay、Extractor 与审计运行；核对只读权限、限量、超时和
  分类错误，不把“Tool 数量”当作能力证明。
- 同一初始症状的两条不同观察产生不同 Tool 调用序列；
  无进展、重复调用、预算耗尽、权限拒绝与模型错误不越界。
- Kubernetes、Prometheus、Loki、Git/CI 外部边界均有真实形态
  测试；可选 Live smoke 使用隔离资源和限权身份，默认 CI 跳过，
  测试未执行的源须在报告标为未验证，不能宣称全通过。
- 按 Trace 验证 LLM/轮次/Tool/Evidence/Result 关联、Prompt/
  Tool 版本和 Case Ground Truth；V0/V1 回归、Alembic 升降级、
  默认 pytest、strict mypy 均通过。
- 在 `docs/roadmap/v2-acceptance.md` 逐项记录环境、日期、
  命令、结果和剩余风险；同步 STATUS/任务状态。

## 不做

- 不把 V2 smoke 当生产就绪认证；API 认证、分布式恢复、
  自动修复和公开部署仍需独立阶段。
- 不运行 Agent 发起的集群或 CI/CD 写操作。

## 验收条件

- [ ] 八类故障与反证/Partial 都有可执行 Case。
- [ ] 10+ 只读 Tool 和所需多数据源经真实边界验证。
- [ ] 动态路径、预算、Policy、Evidence 绑定和审计通过。
- [ ] V0/V1 基线及默认全量测试、strict mypy 均通过。
- [ ] V2 报告与 STATUS 如实区分离线、Live 和未验证项。
