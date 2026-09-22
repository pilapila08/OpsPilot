# V0 Acceptance Report

- Result: Accepted
- Date: 2026-09-22
- Baseline commit: `252bfe50078b7fa63e1d5468ab8f70a53bae9503`
- Contract baseline: [V0 Contract Baseline](../architecture/v0-contract-baseline.md)

## 验收依据

验收以已验证代码和测试为第一事实源，并对照根目录六阶段设计中的 V0 验收标准。原始计划中的示例字段若与已接受 ADR、当前 Schema 或测试不一致，以当前实现为准。

## 验收矩阵

| 原始验收项 | 当前实现 | 可执行证据 | 结果 |
|---|---|---|---|
| AgentState 定义完成 | `AgentState`、`AgentStatus`、显式状态转换与时间线 | `tests/unit/agent/test_state.py` | Pass |
| Intent Schema 完成 | `Target`、`IntentOutput`，严格命名和领域约束 | `tests/unit/agent/test_schemas.py` | Pass |
| Plan Schema 完成 | `PlanStep`、`Plan`，步骤从 1 连续编号且最多 8 步 | `tests/unit/agent/test_schemas.py` | Pass |
| Tool Protocol 完成 | Definition、Descriptor、Invocation、Response、RetryPolicy、Risk Level 与白名单 Registry | `tests/unit/tools/` | Pass |
| Evidence Schema 完成 | 不可变 Evidence、Claim、Verification、缺失证据与冲突模型 | `tests/unit/evidence/` | Pass |
| Error Taxonomy 完成 | 13 个稳定错误码、分类、动作、重试上限和安全降级规则 | `tests/unit/test_errors.py` | Pass |
| 数据库核心表完成设计 | 七张 SQLAlchemy 模型、Alembic 首迁移、外键、索引与 Evidence 追加写保护 | `tests/integration/storage/`、`tests/unit/storage/` | Pass |
| CrashLoopBackOff Case 可复现 | 40 秒慢启动应用、20 秒重启时序、修复清单、五步 ToolResponse Replay 和 Ground Truth | `tests/integration/cases/`、`fixtures/cases/crashloop-liveness-v1/` | Pass |
| 项目目录和开发环境完成 | `src/` 布局、Python 3.12–3.13、pytest、mypy strict、venv + pip | `pyproject.toml`、`docs/development.md` | Pass |

## 验证记录

```text
python -m pytest
114 passed

python -m mypy src tests
Success: no issues found in 34 source files
```

迁移测试覆盖空库升级、回滚、再次升级、ORM 元数据漂移和 PostgreSQL Evidence 触发器 SQL。Case 测试覆盖有界路径加载、ToolResponse 顺序、required Evidence、故障探针时序和修复后恢复状态。

## 相对原始设计的收紧

- 所有跨 Runtime 边界的 Pydantic 模型默认严格、不可变并拒绝额外字段。
- Tool Invocation 增加 `call_id`；Tool Definition 增加版本、来源和显式重试声明。
- Evidence 将原始 `timestamp/confidence` 拆为观察/采集时间和来源/推断/验证置信度。
- 数据库不止完成设计，还提供了可逆迁移和 PostgreSQL 追加写保护。
- 首个 Case 同时提供可选的真实 Kubernetes 复现与默认的确定性 Offline Replay。

这些变化增强了可审计性和可测试性，不改变原始 V0 目标。

## 非阻塞后续项

- Deployment/Service 到 Pod 的确定性解析由 V1 Kubernetes Client Boundary 负责。
- 原始 Tool Result 的压缩与长期保留策略不属于 V1 单故障闭环，在后续存储演进中决定。
- V1 只实现基础确定性 Verifier；复杂 LLM Verifier 与冲突推理由 V4 扩展。
- V0 未运行真实集群集成测试；V1 验收需要增加可选的只读 Kubernetes smoke test。

## 结论

V0 阶段满足全部验收条件。后续开发以 `v0.1` 契约基线继续，任何破坏性调整必须遵守 ADR 0003 的版本和迁移规则。
