# Task Index

一个工作单代表一次边界明确、可独立验收的改动。开发会话开始时读取 `AGENTS.md`、`docs/STATUS.md`、当前工作单和工作单引用的架构文档。

| ID | 任务 | 状态 | 依赖 |
|---|---|---|---|
| V0-001 | [核心状态与 Schema](V0-001-core-state-and-schemas.md) | Done | 无 |
| V0-002 | [Tool Protocol 与 Registry](V0-002-tool-protocol-and-registry.md) | Done | V0-001 |
| V0-003 | [Evidence 与 Error Taxonomy](V0-003-evidence-and-errors.md) | Done | V0-001 |
| V0-004 | [存储模型](V0-004-storage-model.md) | Done | V0-001、V0-003 |
| V0-005 | [首个可复现故障 Case](V0-005-crashloop-case.md) | Done | V0-002、V0-003 |
| V1-001 | [Kubernetes Client Boundary 与目标解析](V1-001-kubernetes-client-boundary.md) | Done | V0-001、V0-002、V0-003 |
| V1-002 | [五个只读 Kubernetes Tool](V1-002-five-readonly-kubernetes-tools.md) | Done | V1-001 |
| V1-003 | [Structured Model Gateway 与 Intent Router](V1-003-model-gateway-and-router.md) | Done | V0-001、V0-003、V0-004 |
| V1-004 | [Planner 与 Plan Validator](V1-004-planner-and-plan-validator.md) | Done | V1-002、V1-003 |
| V1-005 | [Bounded Executor、Evidence Extraction 与持久化](V1-005-executor-evidence-persistence.md) | Done | V0-004、V1-002、V1-004 |
| V1-006 | [CrashLoopBackOff Diagnosis 与基础 Verifier](V1-006-crashloop-diagnosis-verifier.md) | Done | V1-003、V1-005 |
| V1-007 | [Runtime Orchestration 与 Offline E2E](V1-007-runtime-orchestration-offline-e2e.md) | Done | V1-004、V1-005、V1-006 |
| V1-008 | [FastAPI、Live Smoke Test 与 V1 验收](V1-008-fastapi-live-acceptance.md) | Done | V1-007 |
| V2-001 | [多故障架构与验收矩阵](V2-001-architecture-and-case-matrix.md) | Done | V1-008 |
| V2-002 | [版本化 Case、Replay 与 Intent](V2-002-case-replay-and-intent-v2.md) | Done | V2-001 |
| V2-003 | [有界观察驱动 Planner 与 Runtime](V2-003-bounded-observation-runtime.md) | Done | V2-002 |
| V2-004 | [Kubernetes 拓扑、资源与多 Pod 只读 Tool](V2-004-kubernetes-topology-tools.md) | Done | V2-002 |
| V2-005 | [Prometheus 指标与 OOMKilled](V2-005-prometheus-and-oom.md) | Done | V2-003、V2-004 |
| V2-006 | [Readiness Probe Failure](V2-006-readiness-failure.md) | Ready | V2-003、V2-004 |
| V2-007 | [Liveness Probe Failure](V2-007-liveness-failure.md) | Planned | V2-003 |
| V2-008 | [ImagePullBackOff](V2-008-image-pull-failure.md) | Planned | V2-003 |
| V2-009 | [Loki 日志与延迟升高](V2-009-loki-and-latency.md) | Planned | V2-003、V2-005 |
| V2-010 | [Git/CI 元数据与发布后故障](V2-010-git-cicd-post-deployment.md) | In Progress | V2-003、V2-005 |
| V2-011 | [Service 503 拓扑诊断](V2-011-service-503.md) | Done | V2-003、V2-004、V2-005 |
| V2-012 | [八类故障与多数据源阶段验收](V2-012-multi-fault-acceptance.md) | Planned | V2-005 至 V2-011 |

任务状态使用：`Planned`、`Ready`、`In Progress`、`Blocked`、`Done`。
