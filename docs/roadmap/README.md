# 阶段路线图

路线图描述交付顺序，不替代具体工作单。工作按依赖推进；下面保留原始阶段目标，V2 的当前收口取舍以能力矩阵为准，不把暂缓目标记为已验收。

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
V0 已验收 -> V1 已验收 -> V2 多轮 Runtime 与部分离线切片 -> V3 Runtime 工程化（当前优先）
```

V0 已通过[阶段验收](v0-acceptance.md)，`v0.1` 契约基线已经冻结。V1 的离线自动验收、真实集群 Live 诊断及修复侧核验均已完成，详见[V1 验收报告](v1-acceptance.md)。V2 已实现有界观察驱动多轮 Runtime、只读 Tool 和部分故障 Case；八类故障与真实多数据源的阶段总验收尚未完成。[V2 能力范围与计数口径](v2-scope.md)逐类区分 Evidence、Extractor、Verifier、Case 和 Live，[多故障架构与验收矩阵](../architecture/v2-diagnosis-expansion.md)仍是目标协议。V3 首批 Trace 查询、Budget 管理和离线演示已完成，后续设计见 [V3 交接](../handoffs/2026-09-25-v3-runtime.md)。V1 API 没有认证，不应公开部署。

详细阶段需求以根目录 `OpsPilot_六阶段技术设计文档.md` 为原始参考；实施中的最新事实以工作单、架构协议、ADR 和测试为准。
