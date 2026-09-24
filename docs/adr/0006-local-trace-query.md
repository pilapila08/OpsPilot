# ADR 0006: Trace 查询先提供本地 CLI

- Status: Accepted
- Date: 2026-09-25

## Context

OpsPilot 已将 Run、规划轮次、LLM 尝试、Tool 尝试、Evidence 和诊断结果存入同一数据库，但缺少一次查询完整 Trace 的入口。现有 V1 HTTP API 没有认证或租户隔离，直接增加运营级 Trace 端点会扩大敏感运行信息的暴露面。

## Decision

V3-001 提供 `python -m opspilot trace <trace_id>` 本地 CLI，按 `OPSPILOT_DATABASE_URL` 连接已有数据库，只执行读取，不运行迁移，也不创建缺失的 SQLite 数据库。CLI 支持文本和 JSON 输出；不存在的 Trace 返回非零退出码。

TraceView 是白名单投影：保留 Run 状态、版本、预算用量、规划轮次、模型和 Tool 尝试的标识与审计元数据、Evidence 来源与引用、最终结构化诊断和验证摘要。模型请求/响应、原始 Prompt、供应商异常正文、Tool 参数/结果和 Evidence 内容不进入默认输出。V1 Run 明示为 `single-pass`，规划轮次为空，实际调用与预算统计继续显示。

## Consequences

- 本地操作者须有数据库读取权限；数据库路径或连接串从环境提供，CLI 错误不回显连接串。
- HTTP API 暂不增加 Trace 端点。将来需要远程查询时，先设计认证、授权和租户隔离，并复用此安全投影。
- LLM 与 Tool 各自的 `sequence_no` 只在本类别内有序；Round 的 logical call ID 和 Evidence 的具体 Tool attempt ID 表达因果关系，不把时间戳展示顺序误作全局执行序。
