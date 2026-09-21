# V0-005: 可复现的 CrashLoopBackOff Case

- Status: Planned
- Phase: V0
- Depends on: V0-002, V0-003

## 目标

建立首个确定性测试夹具：应用实际启动约 40 秒，而 Liveness Probe 在 10 秒开始检查，导致持续重启。

## 范围

- 创建最小故障应用和 Kubernetes 清单或等价的确定性 Mock Fixture。
- 固定 Pod Status、Events、Previous Logs 和 Deployment 结果样例。
- 给出预期根因和 required Evidence。
- 记录本地复现、清理和离线测试方式。

## 不做

- 不扩展到其他七类故障。
- 不构建完整 UI、RAG 或写操作。
- 不依赖真实生产集群或生产日志。

## 验收条件

- [ ] Case 能稳定产生或模拟 CrashLoopBackOff。
- [ ] Evidence 包含重启次数、Liveness 失败、启动耗时和 Probe 配置。
- [ ] 修复 Probe 后 Case 恢复正常。
- [ ] Fixture 可用于后续集成测试和 Offline Replay。
- [ ] 复现说明与项目状态已更新。

