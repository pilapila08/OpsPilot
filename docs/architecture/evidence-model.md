# Evidence Model

## 目的

Evidence 是诊断事实与其来源之间的可审计连接。最终 Claim 必须引用 Evidence ID，Verifier 负责检查引用是否充分且是否存在冲突。

## 最小 Schema

```json
{
  "evidence_id": "ev_001",
  "trace_id": "trace_001",
  "source": "kubernetes_events",
  "resource": "prod/payment-xxx",
  "observed_at": "2026-09-21T10:00:00Z",
  "collected_at": "2026-09-21T10:00:01Z",
  "content": "Liveness probe failed",
  "confidence": 1.0,
  "tool_call_id": "tc_001"
}
```

## 约束

- `source`、`resource`、观察时间和产生证据的 Tool Call 必须可追溯。
- Tool 的原始结果与 Evidence 摘要应分开保存，避免摘要覆盖原始事实。
- Tool 返回的置信度与模型推断的置信度必须区分。
- Evidence 创建后不可原地改写；纠正信息应生成新版本或新记录。
- Claim 至少包含 `text`、`evidence_ids` 和结论置信度。
- 证据缺失、过期或互相矛盾时，Verifier 必须明确报告。

## 首个 Case 所需证据

```text
Pod restartCount > 0
Events 包含 Liveness probe failed
应用启动耗时大于探针启动时间
Deployment 的 initialDelaySeconds 过小或缺少 startupProbe
```

