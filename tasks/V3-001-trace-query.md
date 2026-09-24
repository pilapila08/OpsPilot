# V3-001: Trace 统一查询与本地 CLI

- Status: Done
- Phase: V3
- Depends on: V1-007、V2-003

## 目标

按 trace_id 一次读取完整审计链，支持本地排障、失败归因与历史 Prompt 版本关联。

## 上下文

- `docs/architecture/storage-model.md`
- `docs/architecture/model-gateway-v1.md`
- `docs/architecture/runtime-v1.md`
- `docs/adr/0005-bounded-observation-planning-v2.md`

## 实现范围

- 审计仓储新增 `calls_for_trace(trace_id)`，返回成功及失败的每次模型尝试，保持稳定顺序和 Trace 隔离。
- 新建只读 TraceView：Run、规划轮次、LLM 尝试（component、prompt_version_id、tokens、cost、latency、error_code）、Tool 尝试（logical call 与重试序号）、Evidence、Verification/Result。
- `python -m opspilot trace <trace_id>` 输出可读文本，提供 JSON 输出供自动验收；未知 Trace 有清晰错误和非零退出码。
- 单独 ADR 记录 CLI 优先：现有 HTTP API 无认证，Trace 的运营信息仅通过本机数据库访问，不增加 HTTP 端点。
- 默认展示审计元数据和已验证结构化结果，不输出凭据、原始 Prompt、模型/provider 异常原文或原始 Tool payload。

## 验收

- [x] V1/V2、成功/失败、Tool 重试及不存在 Trace 均可查询。
- [x] 多 Trace 数据严格隔离，顺序确定，CLI 查询不执行迁移或创建数据库。
- [x] 每次 LLM 尝试可关联历史 Prompt 版本；安全投影不泄漏原始 provider 文本。
- [x] 定向测试、默认 pytest、strict mypy 通过，更新 STATUS；整批交付的交接说明在 V3-003 收尾。

2026-09-25：定向 5 passed；默认 398 passed、4 skipped（3 个 opt-in Live、1 个 Windows symlink）；strict mypy 171 文件通过。既有本机 V1 Replay 数据经 CLI 读取成功。CLI 决策见 ADR 0006。内存审计查询须显式绑定 Run/Trace；SQL 查询由 Run 外键关联。

## 不做

新增 HTTP Trace API、UI、OTel 或跨租户授权系统。
