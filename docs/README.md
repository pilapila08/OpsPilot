# OpsPilot Context Index

本目录保存项目的长期上下文。根目录两份原始文档是项目蓝图；这里的内容是开发过程中维护的当前事实。

## 四层上下文

| 层级 | 位置 | 作用 | 更新频率 |
|---|---|---|---|
| 项目约束 | `../AGENTS.md` | 定义目标、边界和开发规则 | 很低 |
| 架构协议 | `architecture/`、`adr/` | 定义模块契约和技术决策 | 低 |
| 当前状态 | `STATUS.md`、`roadmap/` | 记录阶段、进度、风险和下一步 | 每个任务 |
| 单任务上下文 | `../tasks/` | 限定一次实现的目标和验收条件 | 每个任务 |

## 导航

- [系统总览](architecture/overview.md)
- [Agent State](architecture/agent-state.md)
- [Tool Protocol](architecture/tool-protocol.md)
- [Evidence Model](architecture/evidence-model.md)
- [Error Taxonomy](architecture/error-taxonomy.md)
- [Storage Model](architecture/storage-model.md)
- [V0 Contract Baseline](architecture/v0-contract-baseline.md)
- [V1 Agent MVP](architecture/v1-agent-mvp.md)
- [Kubernetes Tools V1](architecture/kubernetes-tools-v1.md)
- [安全边界](architecture/security-boundary.md)
- [阶段路线图](roadmap/README.md)
- [V0 验收报告](roadmap/v0-acceptance.md)
- [开发环境](development.md)
- [诊断 Case Fixtures](../fixtures/README.md)
- [当前状态](STATUS.md)
- [架构决策](adr/README.md)
- [任务清单](../tasks/README.md)
