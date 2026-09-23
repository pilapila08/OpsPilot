# Project Status

- Updated: 2026-09-23
- Current phase: V1 Agent MVP（Offline 与 Live 均通过；故障到恢复闭环已在真实集群完整验证）
- Current task: `V1-008-fastapi-live-acceptance`（Done）
- Repository state: V0 已验收并冻结为 v0.1；V1-008 已完成验收，包含真实 Live 发现的 Schema 与日志边界修复

## 已完成

- 项目详细计划书与六阶段技术设计文档完成。
- 四层上下文结构初始化。
- 系统边界、Agent State、Tool Protocol、Evidence 和安全协议形成首版文档。
- 接受 ADR 0001：采用受限状态机与 Tool Gateway。
- V0-001 完成：Python 3.12–3.13、`venv + pip`、Pydantic v2、pytest 和 mypy 工程已建立。
- AgentState、Intent、Target、Plan、Budget 及显式状态转换完成，38 项测试通过。
- V0-002 完成：Tool Definition、Invocation、Descriptor、标准响应与白名单 Registry 已实现。
- Risk 0 可受控执行，Risk 1/2 默认拒绝；超时、非法参数、非法输出和内部异常均被安全归一化，完整测试达到 60 项。
- V0-003 完成：不可变 Evidence、Claim、Verification 与缺失/矛盾证据契约已实现。
- 统一 Error Taxonomy 覆盖 LLM、Schema、Tool、权限、Policy、Budget、Context 和外部服务错误，完整测试达到 101 项。
- V0-004 完成：SQLAlchemy 2.0 核心模型、Alembic 首个迁移与 psycopg 3 PostgreSQL 驱动已建立。
- 七张核心表可完整关联任务、Trace、LLM/Tool 调用、Evidence、Prompt 版本和诊断结果；Evidence 由 ORM 与 PostgreSQL 触发器共同保护追加写语义。
- 空库迁移升级、回滚、元数据漂移与 PostgreSQL DDL 验证完成，完整测试达到 108 项。
- V0-005 完成：40 秒慢启动应用、故障/修复 Kubernetes 清单和五步固定 ToolResponse 回放已建立。
- CrashLoopBackOff Ground Truth 包含重启次数、Liveness 失败、启动耗时和 Probe 配置四类 Evidence，并绑定预期 Claim 与 Verification。
- 故障探针约 20 秒触发重启；Startup Probe 修复提供约 50 秒窗口，恢复快照为 Ready 且零重启，完整测试达到 114 项。
- V0 正式验收通过：9 项原始验收标准均有代码与可执行测试支撑，基线提交为 `252bfe50078b7fa63e1d5468ab8f70a53bae9503`。
- 接受 ADR 0003：冻结 v0.1 Runtime、Tool、Evidence、Error、Storage 与 Case 契约，破坏性变更必须版本化。
- V1 Agent MVP 与五个只读 Kubernetes Tool 架构完成，8 个边界明确的工作单已写入 `tasks/`。
- V1-001 完成：官方 Kubernetes Python SDK 被隔离在 `KubernetesReader` / `KubernetesSdkReader` 边界内，支持显式 in-cluster 或指定 kubeconfig/context 配置。
- 严格 `PodTarget`、日志查询限制、去敏错误翻译和 Deployment 到单 Pod 的确定性解析已实现；零候选、多候选和 terminating Pod 均有明确行为。
- Kubernetes 边界 28 项定向测试通过，完整测试达到 142 项，strict mypy 通过。
- V1-002 完成：Pod Status、Events、Current Logs、Previous Logs 和 Deployment 五个 Risk 0 Tool 已通过 `build_kubernetes_registry` 受控注册。
- Tool 输出覆盖 CrashLoop 状态、事件、启动日志和 Probe 配置；Events、UTF-8 日志与环境变量分别完成限长、稳定排序和敏感值清洗。
- Kubernetes 稳定错误可经受信任的 `ToolExecutionError` 投影到统一 Taxonomy，未知异常仍不暴露内部文本；完整测试达到 164 项，strict mypy 通过。
- V1-003 完成：`StructuredModelClient`、`ScriptedModelClient` 与 OpenAI Responses Adapter 共用结构化结果契约，SDK 隐式重试关闭且凭据仅从环境读取。
- Router Prompt 已版本化并绑定 SHA-256；显式 namespace 不进入模型输出 Schema，Schema regeneration 最多两次并消耗 Budget。
- `SQLAlchemyModelAuditRepository` 复用冻结表记录每次成功/失败模型尝试，完整测试达到 186 项，strict mypy 通过。
- V1-004 完成：`ExecutionPlanV1` 与 V0 Plan 独立，AgentState 兼容接收已验证执行计划；Planner 使用版本化 Prompt 和五个 Tool Descriptor 生成四步 Case 计划。
- 纯确定性 Plan Validator 严格检查 Tool 白名单、Risk 0、准确输入模型、namespace/目标、重复调用和剩余预算；通过规范 JSON 封存已验证计划。
- Schema 最多重生成两次，语义无效最多重生成一次；全部尝试写入 `llm_calls`，未知 Tool 和 Policy 拒绝留下安全事件摘要。
- 全量 215 项测试通过，strict mypy 通过；V0 状态反序列化与既有 Router 路径保持兼容。
- V1-005 完成：`BoundedExecutor` 只接受封存的 V1 执行计划，顺序通过 Registry 调用只读 Tool；每次尝试独立审计，分类重试受 Tool Policy 与调用/重试/时间预算共同限制。
- 新迁移保留 V0 首版迁移并扩展 logical call/attempt 字段，旧数据回填且具唯一约束；Tool Call 与 Evidence 以一次尝试为事务单元提交，按 Trace 可查询。
- 五类 Kubernetes 响应有确定性 Evidence Extractor；V0 Case 真实 Handler 四步回放产生四类 required Evidence，不可信日志/Events 文本不进入证据摘要。
- 全量 231 项测试通过，strict mypy 通过；迁移升级/回滚、旧数据、事务回滚、失败保留、重试预算与 Case 集成已验证。
- V1-006 完成：`DiagnosisDraftV1`、审计化候选生成和确定性 CrashLoop Verifier 已实现；模型候选只能引用当前 Trace Evidence，最终事实、建议和置信度由规则决定。
- Verifier 检查 Status、Liveness/重启 Events、Previous Logs 与 Deployment Probe 四类信号及时间窗；缺失、矛盾或执行未完成时持久化 Partial，禁止模型正确猜测越过证据门槛。
- V0 Case 经真实只读 Tool、Executor、Evidence Extractor、候选模型与 Verifier 回放后产生预期 Ground Truth，并可按 Trace 查回 Result/Verification；Evidence 未改写。
- Evidence 布尔属性的 JSON/SQL 往返保真已修复。全量 252 项测试通过，strict mypy 覆盖 103 个源文件。
- V1-007 完成：单一 `DiagnosisRuntime` 串联 Router、Planner、Plan Validator、Executor、Diagnosis Assembler 与 Verifier；Task/Run、阶段状态、预算和结果通过现有仓储落库。
- 严格 Replay Adapter 从 V0 Case 构造全新只读 Registry/Reader，逐步验证 call ID、Tool、参数、namespace、Pod 和故障阶段顺序；Live 模式可替换 Reader 而复用 Runtime。
- Alembic SQLite 离线 E2E 复现 Ground Truth，并按 Trace 查到三次模型调用、四次 Tool 调用、四类 Evidence 和 COMPLETED Result；Partial、预算、Policy、模型失败、Result 写入失败与重复运行隔离均已覆盖。
- 预算终止且已有 Evidence 时不再调用模型或 Tool，仅运行确定性 Verifier 保存 PARTIAL Result；全量 271 项测试通过，strict mypy 覆盖 111 个源文件。
- V1-008 API 主体已实现：FastAPI POST/GET 以严格 DTO 创建与查询诊断；有界 in-process Dispatcher 先持久化 Task，再运行同一 Runtime，支持幂等 key、容量拒绝与有界关闭。
- Replay API 无模型凭据/集群即可端到端返回 Root Cause、四类 Evidence、Recommendation 和 Verification；Live 显式配置与只读 Reader，不静默回退。
- 新增可选 Live smoke test 和操作文档；真实集群手工验证五个只读 Tool 成功。首次 Planner strict schema 被拒；Reader 修复前一次 Runtime 已完成三次模型和四次 Tool 调用，但因日志被 SDK 变成 bytes repr 而输出 PARTIAL。两处阻断修复后，Live 诊断与修复侧核验均已通过，详见 `docs/roadmap/v1-acceptance.md`。
- API 的 COMPLETED、PARTIAL、FAILED、BUDGET_EXCEEDED 与 POLICY_REJECTED 表达均有测试；对外错误仅用固定文案，模型内部文本不泄漏。全量 280 项测试通过、1 项 Live 测试跳过，strict mypy 覆盖 121 个源文件。
- Strict wire 修复后本地全量 284 项测试通过、1 项真实 Live 测试跳过，strict mypy 覆盖 122 个源文件；模拟 Live API 已经过 v2 Planner/Diagnosis、只读 Tool 和 Verifier 产出 COMPLETED。
- Kubernetes Reader 改为以 `_preload_content=False` 读取限量原始日志字节并 UTF-8 解码，避免 SDK 将 bytes 转为 `b'...'` 字符串；边界测试验证真实换行、限量读取和连接释放。最新全量 285 项测试通过、1 项真实 Live 测试跳过，strict mypy 覆盖 122 个源文件。
- 2026-09-23 在已配置的真实集群环境独立复跑 `pytest -m live`：1 passed（43.14s）。五个只读 Tool 与 Live Runtime 端到端诊断通过，最终 `verification.supported=true`。默认离线 285 passed、1 skipped，strict mypy 122 文件通过。
- 2026-09-23 修复侧核验完成：应用 `fixed-deployment.json` 后 rollout 成功，Pod `1/1 Running`、`Ready=True`、`restartCount=0`、`lastState={}`，90 秒后复检仍为 0；新 Pod 无 `Killing` 事件，唯一 `Startup probe failed` 在 40 秒预算内被容忍。结果与 Case 恢复 Ground Truth（Running/ready/max_restart_count 0）一致。
- fixture namespace 已清理，只读 ServiceAccount、Role、RoleBinding 与 token Secret 随之删除，残留资源检查为空。

## 正在进行

- 无。V1-008 已完成：离线验收、真实集群 Live 诊断、修复侧核验与清理全部通过。

## 下一步

1. 拆分 V2 工作单，保持 V1 只读边界。

## 已知风险与待决问题

- V1 只支持单副本 Deployment 到单 Pod 的确定性解析；多副本和滚动发布选择留到 V2。
- Evidence 原始结果的存储格式、压缩和保留周期尚未确定。
- 首次 `ExecutionPlanV1` strict Schema 被真实端点拒收；ADR 0004 已改为扁平封套。后续真实运行已成功完成 Planner 和 Diagnosis，因此此项不再是当前阻断；官方 OpenAI 端点未直接验证。
- SDK 36.0.3 曾将 Previous Logs 字节变成 `repr` 字符串，导致缺少日志 Evidence；Reader 原始字节解码修复后，真实 Live smoke 已通过。外部边界的真实响应形态仍需持续保留专门回归测试。
- V1 API 无认证且进程内队列不提供崩溃恢复，Live smoke 成功不等于可以公开部署或声明生产可用。

## 阻塞项

当前无阻塞项。V1-008 的修复侧核验与清理已完成，`docs/roadmap/v1-acceptance.md` 已登记真实结果。
