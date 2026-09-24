# V2-001: 多故障架构与验收矩阵

- Status: Done
- Phase: V2
- Depends on: V1-008

## 目标

在不改动 V1 冻结契约和代码的前提下，确定 V2 多轮规划、多数据源、
八类故障的 Evidence 门槛和实现依赖，拆出可独立验收的后续工作单。

## 上下文

- `docs/STATUS.md`
- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/adr/0003-freeze-v0-contracts.md`
- `docs/adr/0004-strict-model-wire-envelope.md`
- `OpsPilot_六阶段技术设计文档.md` 的 V2 章节

## 架构交付

- 为 Case、Intent、PlanDecision、ObservationSummary 定义 V2 版本边界。
- 定义 Runtime 拥有的有界观察循环、每轮持久化、全 Run 预算、
  重复请求和无进展停止规则。
- 定义 Kubernetes、Prometheus、Loki、Git/CI 的只读访问范围与
  Tool Schema、Policy、权限和敏感字段边界。
- 列出八类故障的最小 Evidence、反证/缺失判据与明确的 Partial 行为。
- 将新增 Tool 和故障诊断拆成依赖清楚的 V2-002 至 V2-012 工作单。

## 不做

- 不实现 V2 Runtime、Tool、Case 或迁移。
- 不改写 V0 Case、V1 计划、已有 Prompt、Replay 和 Live 验收记录。
- 不把候选架构当作已通过的功能验收。

## 验收条件

- [x] 当前 V1 与原始 V2 目标的差异已明确，八类故障均有证据矩阵。
- [x] ADR 0005 记录多轮规划的长期技术决策与兼容策略。
- [x] 后续工作单均有目标、架构边界、测试和不做范围。
- [x] 状态、任务索引与架构导航同步；文档链接和差异检查通过。

## 完成记录

- 采用 V2 独立版本和 ADR 0005，不放宽 V1 只读、安全、预算及
  Evidence 绑定约束。
- 首个后续任务 V2-002 聚焦 Case/Replay/Router 版本化；其它数据源
  与故障按依赖分批交付。
