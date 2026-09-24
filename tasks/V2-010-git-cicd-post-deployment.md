# V2-010: Git/CI 只读元数据与发布后故障

- Status: In Progress
- Phase: V2
- Depends on: V2-003, V2-005

## 目标

以不可变版本标识和受限时间线连接 Deployment、CI/CD、Git 与
错误率 Evidence，诊断发布后故障，同时避免把时间相关性当因果。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/security-boundary.md`
- `docs/architecture/storage-model.md`
- `docs/adr/0005-bounded-observation-planning-v2.md`

## 架构与实现

- 定义配置侧 repository/project allowlist 与 `GitReader`、
  `CicdReader` 协议；注册 `git.get_recent_commit`、
  `git.diff`、`cicd.get_recent_deployment` 三个 Risk 0 Tool。
  不执行任意 `git` shell、Webhook 或部署动作。
- Git 输入限于已关联仓库的 commit SHA/受限最近窗口；diff 只返回
  文件路径类别、变更量和经清洗的有限结构化摘要，不把源码、
  token、密钥或补丁原文交给模型。CI 只返回 release ID、时间、
  环境、commit/ref 与稳定状态，不输出原始 pipeline 日志。
- 关联 Deployment revision、release ID、commit 与 Prometheus
  error-rate before/after 窗口；时间统一 UTC，时钟偏差与缺字段
  明确标记。单纯“发布后错误变多”最多支持相关性；精确变更根因
  还需直接配置/行为 Evidence。
- Case v2 覆盖发布导致故障、故障早于发布、多个重叠 release、
  未知 commit、无指标和回滚后恢复。

## 测试

- allowlist、路径逃逸、超大 diff、Secret-like 内容、权限拒绝、
  API 超时、分页/时间顺序及错误映射。
- 时间线正例与故障先于部署、重叠发布反证；证据缺失时 Partial。
- Tool 审计不保存敏感 diff 原文，API 不暴露凭据和发布日志。
- 可选 Live smoke 仅连接隔离仓库与测试发布记录。

## 不做

- 不推送代码、不触发/回滚部署、不接受模型提供任意仓库 URL。
- 不把 release 与错误率的时间相关性单独判为根因。

## 验收条件

- [x] 三个 Tool 严格只读且仓库/项目范围可验证。
- [ ] Post-deployment Case 与反证/Partial 分支可回放。
- [x] 变更摘要不泄漏源码秘密，时间线和 Evidence 可审计。
- [x] 默认 pytest、strict mypy 与 STATUS 更新通过。

## 当前实现与剩余门槛

- 已实现固定 GitHub REST GET Reader、配置侧 namespace/Deployment → repository/project/environment allowlist、三个 Risk 0 Tool。diff 只输出类别计数与变更量，补丁、路径、commit message 和 CI 日志不进入 ToolResponse/Evidence。
- CI 时间采用成功 deployment status 的 UTC 时间；曾成功但现为 inactive 的记录保留原成功时间。分页、越界、路径逃逸、非祖先比较、权限/超时和乱序时间均 fail closed。
- 新增确定性发布时间线 Verifier：发布后错误率上升、故障早于发布、重叠发布、缺 commit/指标、以及旧 SHA 重新部署后恢复均有测试。只支持“时间相关”Partial，不把发布当已证实根因。
- `fixtures/cases/release-failure-v2/` 的 `post_release_rise` 分支已通过完整 SQLite Runtime/Planner/Replay/Evidence/Verifier 审计。其余反证目前是单元测试，还需扩为 Case v2 回放分支。
- 既有 `k8s.get_deployment` 不包含可信的运行中 image digest ↔ CI commit/revision 绑定；当前 Verification 显式列出 `deployment_snapshot`、`workload_revision_binding` 和 `direct_causality` 缺口。未实现绑定前不升级为 COMPLETED。
- 可选真实 GitHub/CI Live smoke 未执行，需隔离只读 token 与测试发布记录。
- 当前默认离线回归 `394 passed, 3 skipped`（均为显式 opt-in Live），`mypy src tests` strict 检查 165 个文件通过。
