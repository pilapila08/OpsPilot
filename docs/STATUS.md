# Project Status

- Updated: 2026-09-22
- Current phase: V1 Agent MVP
- Current task: `V1-001-kubernetes-client-boundary`
- Repository state: V0 已验收并冻结为 v0.1；V1 架构与 8 个工作单已拆分

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

## 正在进行

- `V1-001` 已 Ready：实现 Kubernetes SDK 只读边界、公共 Schema、错误翻译和单 Pod 目标解析。

## 下一步

1. 完成 `V1-001`：Kubernetes Client Boundary 与目标解析。
2. 完成 `V1-002`：实现并注册五个只读 Kubernetes Tool。
3. 并行准备 `V1-003`：Structured Model Gateway 与 Intent Router。

## 已知风险与待决问题

- V1 只支持单副本 Deployment 到单 Pod 的确定性解析；多副本和滚动发布选择留到 V2。
- Evidence 原始结果的存储格式、压缩和保留周期尚未确定。
- 首个 Live LLM provider adapter 尚未选定，必须保持 Model Gateway provider-neutral。
- 真实 Kubernetes 只读 smoke test 尚未执行，V1 默认验收先依赖 Offline Replay。

## 阻塞项

当前无阻塞项。
