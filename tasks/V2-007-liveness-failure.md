# V2-007: Liveness Probe Failure 独立诊断

- Status: Planned
- Phase: V2
- Depends on: V2-003

## 目标

将 Liveness Probe Failed 作为与 V1 CrashLoopBackOff 不同的故障族
验证：判断 liveness 是否触发重启及其配置/应用健康原因，不把
所有重启都归咎于 40 秒慢启动。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/diagnosis-v1.md`
- `fixtures/cases/crashloop-liveness-v1/`

## 架构与实现

- Case v2 包含启动期 liveness 失败、运行一段时间后 liveness
  失败、只有 readiness 失败三个可区分观察分支。
- 从 Pod Status、Events、previous/current logs 和 Deployment
  probe 提取时间顺序、restart count、failure threshold 和
  startup probe 存在性，不复制不可信日志原文。
- Verifier 要求 liveness-triggered termination 与配置/健康证据
  同时出现；不能仅凭 Event 文本、单次探测失败或旧 Pod 事件下
  精确根因。若探针失败被 startup 预算容忍且零重启，应判为
  健康恢复而非故障。
- 与 V1 CrashLoop Verifier 同场景回放时，V1 的既有结论和
  Evidence ID 绑定方式保持不变。

## 测试

- 启动过慢、运行时失活、readiness-only、startup probe 容忍
  失败四类场景。
- 旧/新 Pod 事件按 UID 与时间隔离；无 Killing、零重启或
  probe 窗口足够时不得给出错误 liveness 根因。
- 证据缺失/相互矛盾产生 Partial，不触发模型补猜。

## 不做

- 不改写 V0 Case Ground Truth 或 V1 的确定性规则。
- 不由 Agent 应用 probe 修复清单。

## 验收条件

- [ ] Liveness 与 CrashLoop 根因边界有可执行正反例。
- [ ] Event/Pod 身份和时间窗均被确定性校验。
- [ ] V1 Live/Replay 回归与 V2 离线 Case 通过。
- [ ] 默认 pytest、strict mypy 与 STATUS 更新通过。
