# 阶段路线图

路线图描述交付顺序，不替代具体工作单。阶段必须按依赖推进，每个阶段达到验收条件后再扩大范围。

| 阶段 | 目标 | 核心验收 |
|---|---|---|
| V0 | 固定 Runtime 协议和本地环境 | State、Schema、Tool、Evidence、Error 和首个 Case 就绪 |
| V1 | 跑通单故障诊断闭环 | CrashLoopBackOff 端到端成功，结果包含根因、证据和建议 |
| V2 | 扩展多故障和多数据源 | 8 类故障、10+ Tool、Planner 能根据观察动态调整路径 |
| V3 | Runtime 工程化 | Trace、Replay、Budget、版本、成本和分类重试可用 |
| V4 | 可信与安全 | Evidence Verifier、Policy、RBAC、审批和注入防御生效 |
| V5 | 产品化和部署 | UI、OTel、Dashboard、Compose、Helm、CI/CD 和评测集完成 |

## 当前优先级

```text
V0 -> V1 -> V2 -> V3 -> V4 -> V5
```

V0 已通过[阶段验收](v0-acceptance.md)，`v0.1` 契约基线已经冻结。V1 已拆分为 8 个工作单，当前从 `V1-001` Kubernetes Client Boundary 开始；仍不提前引入 RAG、MCP、Multi-Agent、写操作或完整 React 前端。

详细阶段需求以根目录 `OpsPilot_六阶段技术设计文档.md` 为原始参考；实施中的最新事实以工作单、架构协议、ADR 和测试为准。
