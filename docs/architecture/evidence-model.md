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
  "source_confidence": 1.0,
  "tool_call_id": "call_001",
  "raw_result_ref": "result_001",
  "attributes": [
    {
      "key": "reason",
      "value": "Unhealthy"
    }
  ]
}
```

## 约束

- `source`、`resource`、观察时间和产生证据的 Tool Call 必须可追溯。
- Tool 的原始结果与 Evidence 摘要应分开保存，避免摘要覆盖原始事实。
- `source_confidence`、`inference_confidence` 和 `verification_confidence` 必须区分。
- Evidence 及其扩展属性创建后不可原地改写；纠正信息应生成新版本或新记录。
- `observed_at` 与 `collected_at` 必须携带时区，且观察时间不得晚于采集时间。
- `raw_result_ref` 可引用独立保存的原始 Tool Result；V0 不实现持久化。
- Claim 至少包含 `text`、`evidence_ids` 和结论置信度。
- 证据缺失、过期或互相矛盾时，Verifier 必须明确报告。

## Claim Schema

```json
{
  "claim_id": "claim_001",
  "text": "Liveness Probe 配置过早",
  "evidence_ids": ["ev_001", "ev_002"],
  "inference_confidence": 0.91
}
```

Claim 必须引用至少一条 Evidence，且同一 Claim 中 Evidence ID 不得重复。

## Verification Schema

```json
{
  "claim_id": "claim_001",
  "supported": false,
  "verification_confidence": 0.88,
  "checked_evidence_ids": ["ev_001", "ev_002"],
  "missing_evidence": [
    {
      "requirement": "previous container logs",
      "reason": "previous log stream was unavailable"
    }
  ],
  "contradictions": [],
  "rationale": "当前证据不足以确认根因"
}
```

- `supported=true` 时不能同时报告缺失或矛盾证据。
- `supported=false` 时必须至少报告一项缺失证据或矛盾。
- 矛盾只能引用 `checked_evidence_ids` 中已经检查的 Evidence。

## 首个 Case 所需证据

```text
Pod restartCount > 0
Events 包含 Liveness probe failed
应用启动耗时大于探针启动时间
Deployment 的 initialDelaySeconds 过小或缺少 startupProbe
```
