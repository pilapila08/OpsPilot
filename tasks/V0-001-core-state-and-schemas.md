# V0-001: 核心状态与 Structured Output Schema

- Status: Done
- Phase: V0
- Completed: 2026-09-21

## 目标

初始化最小 Python 工程，实现 `AgentState`、状态枚举、Intent、Plan 和 Budget 的 Pydantic Schema，并通过测试固定基础协议。

## 上下文

- `docs/architecture/overview.md`
- `docs/architecture/agent-state.md`
- `docs/adr/0001-bounded-runtime-and-tool-gateway.md`

## 范围

- 选择并记录 Python 版本和依赖管理方式。
- 建立最小包结构、测试目录和静态检查配置。
- 实现 AgentStatus 及合法状态转换。
- 实现 Intent、Target、Plan、PlanStep 和 Budget Schema。
- 验证最大步骤等边界约束和序列化行为。

## 不做

- 不调用真实 LLM。
- 不接入 Kubernetes、PostgreSQL 或 Redis。
- 不实现 Planner、Executor 或 API。
- 不引入 LangGraph，除非先通过 ADR 明确必要性。

## 验收条件

- [x] Schema 可以序列化与反序列化。
- [x] 非法 intent、domain、资源名和预算被拒绝。
- [x] Plan 超过最大步骤时被拒绝。
- [x] 合法状态转换通过，终止状态后继续执行被拒绝。
- [x] 单元测试和静态检查通过。
- [x] `docs/STATUS.md` 已更新。

## 验证结果

- `python -m pytest`：38 passed
- `python -m mypy src tests`：Success, no issues found
