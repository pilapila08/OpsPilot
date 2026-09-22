# Project Status

- Updated: 2026-09-22
- Current phase: V0 Design
- Current task: `V0-004-storage-model`
- Repository state: V0 核心状态、Tool Registry、Evidence Contract 与统一错误分类已实现

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

## 正在进行

- V0 PostgreSQL 核心存储模型设计。

## 下一步

1. 完成 `V0-004`：确定 PostgreSQL 核心表及迁移方案。
2. 完成 `V0-005`：构建可复现的 CrashLoopBackOff Mock Case。

## 已知风险与待决问题

- 数据库 ORM 与迁移工具尚未选择。
- Service/Deployment 到具体 Pod 的资源解析规则尚未设计。
- Evidence 原始结果的存储格式、压缩和保留周期尚未确定。
- Verifier 的确定性规则与 LLM 判断边界尚未确定。

## 阻塞项

当前无阻塞项。
