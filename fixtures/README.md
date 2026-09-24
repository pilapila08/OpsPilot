# Diagnostic Case Fixtures

`fixtures/cases/` 保存可版本化、可离线回放的确定性诊断 Case。V1 Case 使用 `opspilot.cases.load_case`；新代码可使用显式版本分发的 `load_case_versioned`，V2 Case 使用 `load_case_v2`。Case 引用同目录中的固定 ToolResponse、Evidence、预期诊断与可选恢复结果。

当前 Case：

- [crashloop-liveness-v1](cases/crashloop-liveness-v1/README.md)：慢启动应用被过早的 Liveness Probe 持续重启。
- [restart-branches-v2](cases/restart-branches-v2/README.md)：同一 Pod 重启症状下的 OOM、Probe 缺证据与健康反证 Replay 分支。
