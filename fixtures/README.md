# Diagnostic Case Fixtures

`fixtures/cases/` 保存可版本化、可离线回放的确定性诊断 Case。每个 Case 的 `case.json` 必须通过 `opspilot.cases.load_case` 加载，并引用同目录中的固定 ToolResponse、Evidence、预期诊断和恢复结果。

当前 Case：

- [crashloop-liveness-v1](cases/crashloop-liveness-v1/README.md)：慢启动应用被过早的 Liveness Probe 持续重启。
