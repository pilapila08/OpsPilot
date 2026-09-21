# V0-002: Tool Protocol 与 Registry

- Status: Planned
- Phase: V0
- Depends on: V0-001

## 目标

实现类型安全的 Tool Definition、Tool Invocation、标准响应和 Registry，固定 Executor 与外部能力之间的契约。

## 上下文

- `docs/architecture/tool-protocol.md`
- `docs/architecture/security-boundary.md`
- `docs/adr/0001-bounded-runtime-and-tool-gateway.md`

## 范围

- 定义 Tool 风险等级、超时和重试策略。
- 实现 Tool 注册、重复名称检测和白名单查找。
- 使用 Pydantic 校验调用参数与返回值。
- 使用假 Tool 验证成功、超时、非法参数和不存在工具。

## 不做

- 不连接真实 Kubernetes。
- 不提供任意 Shell Tool。
- 不实现 Risk 1/2 的真实执行能力。

## 验收条件

- [ ] 未注册 Tool 被拒绝。
- [ ] 输入输出均经过 Schema 校验。
- [ ] 重复注册和非法风险等级被拒绝。
- [ ] 错误被规范化，不泄露敏感内部信息。
- [ ] 单元测试通过并更新项目状态。

