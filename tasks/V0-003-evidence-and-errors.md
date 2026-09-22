# V0-003: Evidence、Verifier Contract 与 Error Taxonomy

- Status: Done
- Phase: V0
- Depends on: V0-001
- Completed: 2026-09-22

## 目标

实现可追溯 Evidence、Claim、Verification Schema 和统一错误分类，为 Executor 与 Verifier 提供稳定契约。

## 上下文

- `docs/architecture/evidence-model.md`
- `docs/architecture/security-boundary.md`

## 范围

- 实现 Evidence、Claim、Verification 和矛盾/缺失证据字段。
- 区分观察时间与采集时间、来源置信度与推断置信度。
- 定义 LLM、Schema、Tool、权限、Policy、Budget 和外部服务错误。
- 定义各错误是否允许重试及默认处理策略。

## 不做

- 不实现基于 LLM 的完整 Verifier。
- 不接入向量数据库或 RAG。
- 不实现 Evidence 持久化。

## 验收条件

- [x] Claim 必须引用至少一条 Evidence。
- [x] Evidence 可追溯到 trace 和 Tool Call。
- [x] 非法置信度与时间字段被拒绝。
- [x] 错误分类和重试策略有参数化测试。
- [x] 单元测试通过并更新项目状态。

## 验证结果

- `python -m pytest`：101 passed
- `python -m mypy src tests`：Success, no issues found
