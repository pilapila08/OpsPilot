# V2-002: 版本化 Case、Replay 与 Intent

- Status: Done
- Phase: V2
- Depends on: V2-001

## 目标

建立 V2 多故障的 Ground Truth 和离线 Replay 基础，让不同观察分支
能够在无外部凭据环境中确定性复现；Router 可提出八类故障假设与
Pod/Deployment/Service/Ingress 目标，但不得将假设当成事实。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/adr/0003-freeze-v0-contracts.md`
- `docs/architecture/runtime-v1.md`
- `src/opspilot/cases/models.py`
- `src/opspilot/routing/`

## 架构与实现

- 新建 `CaseDefinitionV2` 与独立 Loader；版本判别只分发 v1/v2，
  不原地扩宽 V0 `RequiredSignal` 或 `CaseDefinition`。
- Case 声明 fault family、目标类型、允许的 Tool/数据源、证据要求、
  反证、分支图、预期 Partial/Completed、可选恢复判据。引用路径仍
  限于 Case 目录，response 的 Tool 名/参数/call ID/来源须匹配。
- Replay 在同一初始症状下支持至少两条明确允许的观察分支；未声明
  Tool、跨 namespace、错目标、错时间窗或多余调用 fail closed。
  不把真实生产日志、密钥或 token 放进 fixture。
- 新建严格 `IntentV2` 与版本化 Router Prompt；显式 namespace
  由 API 输入确定，不从模型覆盖。无明确资源/目标时返回结构化
  澄清/Partial，而不猜测 Pod。
- 保持 V1 API 的 `case_id` 与 Replay 行为可用；V2 路由选择
  必须显式，不根据旧 Case 文件名猜版本。

## 测试

- v1 Case 原样加载和 V1 Offline E2E 不变。
- v2 Case 正常加载；路径逃逸、重复 call ID、缺失 Ground Truth、
  不允许的分支或 Tool 响应错位均拒绝。
- 同一症状的两条 Replay 分支走不同允许调用；非声明调用拒绝。
- Router 正常、多义/未知目标、Schema 错误与 Prompt 注入文本测试。

## 不做

- 不接真实 Prometheus/Loki/Git，也不实现多轮 Runtime。
- 不让 Router 自动放宽 Tool Policy 或读取 Secret。

## 验收条件

- [x] V2 Case/Intent 严格 Schema 与版本分发可用，V1 数据无迁移负担。
- [x] 两分支 Replay、负向/缺失证据 fixture 和边界测试通过。
- [x] Router 的目标与故障假设受限且可审计。
- [x] 默认 pytest 与 strict mypy 通过，更新 STATUS。

## 完成记录

- 新增独立 CaseDefinitionV2、显式版本 Loader 与逐调用精确匹配的 ReplaySessionV2；旧 Case 与 V1 Runtime 保持原样。
- 同一初始 Pod 状态观察对应 OOM、probe 缺证据和健康反证三条分支；fixture 响应符合现有 Kubernetes Tool 输出模型。
- V2 Router 限定八类故障假设与四类目标，namespace 仅取调用方输入；澄清、Schema 重试、越界拒绝均可审计。
- 默认 pytest：317 passed、1 skipped（opt-in Live）；strict mypy：127 source files。
- V2 API/Runtime 接线与多轮规划不在本单范围，留给 V2-003。
