# V1-006: CrashLoopBackOff Diagnosis 与基础 Verifier

- Status: Ready
- Phase: V1
- Depends on: V1-003, V1-005

## 目标

为首个 CrashLoopBackOff Case 生成结构化候选诊断，并使用确定性基础 Verifier 检查 Claim 是否被当前 Trace 的 Evidence 支持；证据不足时输出 Partial Diagnosis。

## 上下文

- `docs/architecture/v1-agent-mvp.md`
- `docs/architecture/evidence-model.md`
- `fixtures/cases/crashloop-liveness-v1/case.json`
- `docs/architecture/v0-contract-baseline.md`
- `docs/architecture/execution-v1.md`

## 架构位置

```text
Intent + Execution Summary + Evidence
  -> Diagnosis Assembler
       -> StructuredModelClient (candidate only)
  -> Basic CrashLoop Verifier (deterministic authority)
  -> Diagnosis Result
```

LLM 只能提出候选 Claim、root cause 和 recommendation。是否支持以及最终是 COMPLETED 还是 PARTIAL 由代码规则决定。

## 候选诊断 Schema

新增版本化 `DiagnosisDraftV1`：

```text
schema_version = 1
root_cause
recommendation
claims: tuple[Claim, ...]
```

每个 Claim 必须引用传给模型的 Evidence ID。禁止模型生成新 Evidence、改写 content 或引用其他 Trace。

## CrashLoop 规则

完成结论 `Liveness Probe 配置过早` 需要四类信号：

1. Pod Status：`restart_count > 0` 且当前/近期状态表明 CrashLoopBackOff 或反复终止。
2. Events：存在 Liveness failure，并有容器重启/Killing 关联。
3. Previous Logs：应用声明/观察到的启动时长，且在 Ready 前被终止。
4. Deployment：Liveness 的有效失败窗口早于应用启动完成，且无足够 Startup Probe 保护。

基础通用底线仍是至少 2 条 Evidence 且没有直接冲突；但首个 Case 要输出 COMPLETED 必须满足全部四类信号。缺少任一 required signal 时返回 PARTIAL，并填充 `MissingEvidence`。

## 时间计算

Verifier 用结构化属性计算，不解析自由文本：

```text
liveness_restart_at
  = initial_delay_seconds
  + period_seconds * (failure_threshold - 1)

startup_budget
  = startup_probe.period_seconds
  * startup_probe.failure_threshold
```

仅当 observed startup duration 大于未保护的 liveness restart time，或修复后的 startup budget 足够时，才能做对应判断。单位缺失或字段冲突时不得猜测。

## 冲突处理

- restart count 为 0 与 CrashLoop Claim 冲突。
- Events 明确为 Readiness 而非 Liveness 时不能作为 Liveness 证据。
- 日志显示启动已在 Liveness 前完成时构成冲突。
- Deployment 已有足够 Startup Probe 时构成配置根因冲突。
- Contradiction 只能引用已检查 Evidence ID。

存在直接冲突时返回 PARTIAL；V1 不让 LLM裁决冲突。

## Recommendation

Recommendation 必须与验证事实一致，可建议：

- 添加/扩大 Startup Probe。
- 将 Liveness 延后到应用可启动后。
- 继续采集缺失的 Previous Logs 或 Deployment Probe。

不得建议直接修改集群、自动重启、删除 Pod 或执行命令。

## 测试策略

- V0 Ground Truth 四类 Evidence -> COMPLETED，Claim/Verification 与 expected diagnosis 对齐。
- 每次移除一种 required Evidence -> PARTIAL + 对应 MissingEvidence。
- 对四类冲突逐一测试。
- LLM 引用未知 Evidence、其他 Trace 或重复 ID 被拒绝。
- 模型给出正确 Root Cause 但 Evidence 不足时仍为 PARTIAL。
- 验证结果和诊断结果可持久化，Evidence 本身未被修改。

## 不做

- 不支持 OOMKilled、ImagePullBackOff 或其他故障。
- 不实现 LLM Verifier、RAG、概率融合或多 Claim 复杂图。
- 不执行 Recommendation。
- 不把自由文本匹配作为唯一事实依据。

## 验收条件

- [ ] 候选诊断只能引用当前 Trace 的现有 Evidence。
- [ ] 四类 required signal 全部满足时输出 COMPLETED。
- [ ] 缺失或冲突时输出 PARTIAL，并明确 MissingEvidence/Contradiction。
- [ ] Ground Truth Case 得到预期 Root Cause、Evidence 和 Recommendation。
- [ ] Recommendation 不包含写操作或任意命令。
- [ ] Result 与 Verification 持久化并可按 Trace 查询。
- [ ] 单元测试、Case 参数化测试和 strict mypy 通过。
