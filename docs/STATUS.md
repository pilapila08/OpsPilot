# Project Status

- Updated: 2026-09-21
- Current phase: V0 Design
- Current task: `V0-002-tool-protocol-and-registry`
- Repository state: Python 工程及 V0 核心状态与 Schema 已实现

## 已完成

- 项目详细计划书与六阶段技术设计文档完成。
- 四层上下文结构初始化。
- 系统边界、Agent State、Tool Protocol、Evidence 和安全协议形成首版文档。
- 接受 ADR 0001：采用受限状态机与 Tool Gateway。
- V0-001 完成：Python 3.12–3.13、`venv + pip`、Pydantic v2、pytest 和 mypy 工程已建立。
- AgentState、Intent、Target、Plan、Budget 及显式状态转换完成，38 项测试通过。

## 正在进行

- V0 Tool Protocol 与 Registry 设计。

## 下一步

1. 完成 `V0-002`：实现 Tool Definition、Registry 和调用协议。
2. 完成 `V0-003`：实现 Evidence、Claim、Verification 和错误分类。
3. 完成 `V0-004`：确定 PostgreSQL 核心表及迁移方案。
4. 完成 `V0-005`：构建可复现的 CrashLoopBackOff Mock Case。

## 已知风险与待决问题

- 数据库 ORM 与迁移工具尚未选择。
- Service/Deployment 到具体 Pod 的资源解析规则尚未设计。
- Evidence 原始结果的存储格式、压缩和保留周期尚未确定。
- Verifier 的确定性规则与 LLM 判断边界尚未确定。

## 阻塞项

当前无阻塞项。
