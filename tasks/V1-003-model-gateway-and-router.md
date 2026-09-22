# V1-003: Structured Model Gateway 与 Intent Router

- Status: Planned
- Phase: V1
- Depends on: V0-001, V0-003, V0-004

## 目标

建立 provider-neutral 的结构化模型调用边界，并实现 V1 Intent Router，使自然语言请求稳定映射为现有 `IntentOutput`，同时记录 Prompt/Model 版本、Token、成本、延迟和错误。

## 上下文

- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/agent-state.md`
- `docs/architecture/error-taxonomy.md`
- `docs/architecture/storage-model.md`
- `docs/architecture/v0-contract-baseline.md`

## 架构位置

```text
Router
  -> StructuredModelClient Protocol
       -> Provider Adapter

Tests / Offline Replay
  -> ScriptedModelClient
```

Router 不导入 provider SDK。Provider Adapter 的选择和模型名称来自配置；若首个 provider 引入长期绑定，工作单中补充 ADR。

## 范围

1. 定义 `StructuredModelClient`：
   - 输入消息、目标 Pydantic Schema、model config、Prompt version 和 timeout。
   - 输出已验证对象及 usage/latency/provider metadata。
   - 不向上层返回 provider 原始对象。
2. 实现 `ScriptedModelClient`：
   - 测试中按调用顺序返回固定结构化结果或错误。
   - 支持断言请求 Schema、Prompt version 与调用次数。
3. 实现一个可配置 Live Provider Adapter：
   - 凭据只来自环境/秘密配置。
   - Model 名称、timeout、temperature 和 token limit 外部化。
   - 统一映射 rate limit、timeout、context 和其他外部错误。
4. Prompt Version：
   - Router prompt 以版本化文件保存。
   - 内容 hash 与版本写入 `prompt_versions`。
5. Intent Router：
   - 输入 query 与显式 namespace。
   - 输出现有 `IntentOutput`。
   - V1 限制 `intent=diagnose`、Kubernetes restart/CrashLoop 场景。
   - namespace 参数优先于模型推断，模型不得切换到其他 namespace。
6. Schema regeneration：
   - 仅 `SCHEMA_VALIDATION` 最多重试 2 次。
   - regeneration 消息只描述 Schema 错误，不回显敏感输入。
7. 每次尝试追加 `llm_calls`，包括失败尝试。

## Router 失败行为

- query 空/超长：`INVALID_ARGUMENT`。
- 模型两次后仍不符合 Schema：`SCHEMA_VALIDATION` + FAILED。
- rate limit/timeout：按 Taxonomy 和预算决定重试。
- 不支持的 intent/domain/problem type：稳定拒绝，不自动扩大 V1 范围。
- namespace 缺失：API 层要求补充，不默认使用 `default`、`prod` 或当前 context。

## 测试策略

- Scripted client 覆盖正常结果、一次 regeneration 后成功和超过上限。
- Provider Adapter 使用 HTTP/SDK Fake，不发送真实请求。
- 验证 Prompt version/hash、LLM Call 记录和 Token/成本累计。
- Prompt Injection 样例只能作为 query data，不能改变 namespace、Schema 或系统边界。
- 凭据和 provider 异常正文不得进入 ErrorInfo、Trace 或快照。

## 不做

- 不实现 Planner、Tool Calling 或 Verifier。
- 不实现多模型路由、fallback、embedding 或 RAG。
- 不将 Prompt 写死在 Python 字符串中。
- 不允许测试依赖真实付费 LLM。

## 验收条件

- [ ] Router 只依赖 StructuredModelClient Protocol。
- [ ] Live 与 Scripted Adapter 使用同一结构化结果契约。
- [ ] Router 对 V0 Case 请求产生预期 `IntentOutput`。
- [ ] Schema regeneration 最多 2 次且受预算约束。
- [ ] 每次模型尝试均记录版本、usage、延迟和结果状态。
- [ ] 错误遵循统一 Taxonomy 且不泄漏凭据。
- [ ] 单元测试、存储集成测试和 strict mypy 通过。
