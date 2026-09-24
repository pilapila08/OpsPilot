# V3-003: Offline Replay 端到端演示

- Status: Done
- Phase: V3
- Depends on: V3-001、V3-002

## 目标

一条命令完成无密钥、无集群的真实 Runtime Replay，并通过 Trace 查询呈现完整审计链，支持三分钟演示。

## 上下文

- `tasks/V3-001-trace-query.md`
- `tasks/V3-002-budget-manager.md`
- `fixtures/cases/crashloop-liveness-v1/README.md`
- `docs/development.md`

## 实现范围

- 添加 `[project.scripts]`，提供 `opspilot demo` 和等价 `python -m opspilot demo`。
- 默认运行内置 CrashLoop Case；独立本地 SQLite 存储、显式迁移、全新 Task/Run/Trace 标识，可重复运行。
- 调用已有 Runtime、Replay 与 Trace 组装器，CLI 不复制诊断/预算/审计逻辑。
- 输出轮次、每次模型调用的 Prompt 版本/token/成本/延迟/错误、每次 Tool 尝试、四类 required Evidence、Verification 和停止原因。
- 明确 Replay 的模型 usage 为 fixture/scripted 用量，不是实际付费推理成本。
- 文档提供三分钟步骤及“成本控制/模型失败/Prompt 验证”证据映射，当前缺口按实际代码记录。

## 验收

- [x] 清空模型/集群环境后单命令端到端完成。
- [x] COMPLETED 与受预算限制的 PARTIAL 演示可重复运行并查询 Trace。
- [x] 输出可读且不泄漏原始敏感内容；CLI 退出码与结果明确。
- [x] CLI 集成测试、默认 pytest、strict mypy 通过。
- [x] 开发文档、STATUS 和下一位 AI 的交接文档完整。

## 验收结果

2026-09-25：5 项 Demo CLI 集成测试通过，默认 431 passed、4 skipped，strict mypy 177 文件通过。editable 安装后的 `opspilot.exe demo --scenario oom` 已实跑 COMPLETED；`--max-steps 1` 在第二轮 Planner 前产生 PARTIAL。默认 CrashLoop 为四类、五条 Evidence；额外 OOM 场景展示四轮规划。完整操作与退出码见 `docs/development.md`，交接见 `docs/handoffs/2026-09-25-v3-runtime.md`。

## 不做

新的推理引擎、假造 Live 成本或公开 Demo API。
