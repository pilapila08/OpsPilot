# V2 能力范围与阶段取舍

- 审计日期：2026-09-25
- 代码基线：`origin/main` 的 `903b8c9`。本页按已同步代码、测试和工作单记录事实；后续分支应重新核对，不沿用本页计数。
- 阶段性质：V2 的优先技术增量是**有界、观察驱动的多轮 Runtime**。它已能根据上一轮 Evidence 选择下一轮 Tool，并记录轮次、调用、预算与结果（`src/opspilot/runtime/v2.py`、`tests/integration/runtime/test_v2_observation_e2e.py:123-198`）。这项完成不等于八类故障均完成诊断。

## 计数口径

八类故障来自 `docs/architecture/v2-diagnosis-expansion.md:110-125`。下列数字分别回答不同问题，不能相加或互换：

| 口径 | 当前结果 | 证据 |
|---|---:|---|
| Router 可提出的 V2 故障假设 | 8/8 | `src/opspilot/faults.py:5-14`、`tests/unit/routing/test_v2_router.py:88-112` |
| 有专用 V2 确定性 Verifier 的故障类 | 3/8 | `src/opspilot/diagnosis/oom_v2.py:17`、`service_503_v2.py:53`、`release_v2.py:26` |
| 有 V2 Case 文件的故障类 | 4/8 | `fixtures/cases/restart-branches-v2/`、`service-503-v2/`、`release-failure-v2/` |
| 有 V2 Runtime Replay 且至少一个特定子因可输出受支持结论的故障类 | 2/8 | OOM 的 `peak_present`；Service 503 的 `no_ready`、`selector_mismatch`。见下表；只表示这些离线分支，不表示整类或 V2 阶段验收完成 |
| 完整 V2 诊断链路的真实环境 Live 验证 | 0/8 | 现有 Live 文件仅覆盖 V1 API、V2 Kubernetes Tool 和单个 Prometheus 指标；没有 V2 Runtime Live 端到端测试 |

`ObservationRuntimeV2` 在构造时注入一个 `V2Verifier`（`src/opspilot/runtime/v2.py:71-87,256-270`）；架构文档提出的按故障类选择 Verifier 的 registry 尚未落地。V2-012 的八类、多数据源、真实边界整体验收仍全部未勾选（`tasks/V2-012-multi-fault-acceptance.md:46-52`）。

当前有意保留五个优先方向：**CrashLoopBackOff、Liveness Probe Failed、OOMKilled、Service 503、Post-deployment failure**。这是后续诊断能力的范围选择，**不是“五类已完成”**。CrashLoopBackOff 的完整验证属于 V1；OOMKilled 和 Service 503 仅有上述离线受支持子因；独立 Liveness 尚无 V2 Verifier，发布后故障目前只能给出 Partial。

## 八类故障的实际证据链

表中“Live”指**该故障类的 V2 诊断端到端**验证；单个 Tool 的可选 smoke 不算该项完成。

| 故障类 | Evidence | Extractor | Verifier | Case 分支 | Live | 完成口径 |
|---|---|---|---|---|---|---|
| CrashLoopBackOff（早期 Liveness） | V1 Pod 重启、Liveness Event、Previous Logs、Deployment Probe | V1 `src/opspilot/evidence/extractors.py:132-266` | V1 `BasicCrashLoopVerifier` 可给受支持结论；V2 无独立规则（`src/opspilot/diagnosis/verifier.py:19-155`） | V1 CrashLoop Case 有支持与缺证据分支，Runtime Replay 见 `tests/integration/runtime/test_offline_e2e.py:141-185`；无独立 V2 Case | V1 完整 API Live 已验证（`docs/roadmap/v1-acceptance.md:77-101`）；V2 无 | **V1 完成**，不计入 V2 的 2/8 |
| Liveness Probe Failed | 可复用 V1 Liveness Event、重启与 Probe 信号 | V1 `src/opspilot/evidence/extractors.py:132-266` | 无独立 V2 规则 | V2 `probe_branch`、`healthy_branch` 预期均 Partial；仅 `probe_branch` 由测试用 `PartialVerifier` 跑 Runtime E2E，证明动态规划（`tests/integration/runtime/test_v2_observation_e2e.py:123-175`） | 无 V2 Live | V2 独立诊断未完成 |
| Readiness Probe Failed | Pod Ready condition、readiness Event、Deployment readiness probe；缺具体 Pod 到 EndpointSlice 关联与精确子因 | V2 `src/opspilot/evidence/v2.py:59-135` | 无 | 无 Case 分支或恢复快照（`tasks/V2-006-readiness-failure.md:46-58`） | 无 | 部分 Evidence 已实现，暂停且未验收 |
| OOMKilled | Pod OOM termination、同容器内存上限、终止前 Prometheus memory sample | V2 `src/opspilot/evidence/v2.py:79-96,136-148,291-342` | `OomKilledVerifierV2` 要求三个匹配信号（`src/opspilot/diagnosis/oom_v2.py:17-140`） | `peak_present` COMPLETED、`peak_missing` PARTIAL，均经 SQLite Runtime Replay（`tests/integration/runtime/test_v2_observation_e2e.py:201-284`） | 无 V2 诊断 Live；Prometheus 单指标 smoke 未真实执行 | **离线一个特定子因受支持** |
| ImagePullBackOff | Router 有名称；无镜像不存在、鉴权失败、超时的分类 Evidence | 现有 Status/Event 提取器不形成上述分类（`src/opspilot/evidence/extractors.py:132-184`） | 无 | 无（`tasks/V2-008-image-pull-failure.md:44-49`） | 无 | 未实现故障诊断 |
| Service 503 | Ingress route、Service、EndpointSlice、Pod membership、Service scoped 503 指标 | `src/opspilot/evidence/extractors.py:269-335`；`src/opspilot/evidence/v2.py:152-197,291-342` | `Service503VerifierV2` 支持无 ready 后端与稳定 selector 错配；端口异常只给 Partial（`src/opspilot/diagnosis/service_503_v2.py:53-239`） | `no_ready`、`selector_mismatch` COMPLETED；`healthy_backend`、`port_anomaly` PARTIAL，四分支 SQLite Replay（`tests/integration/runtime/test_service_503_replay.py:57-64`） | 无 V2 诊断 Live；仅可选 Tool smoke | **离线两个特定子因受支持**；端口因果待证 |
| Latency increase | Prometheus latency 指标；缺 Loki 日志信号 | 通用指标 `src/opspilot/evidence/v2.py:291-342`；Prometheus Tool `src/opspilot/tools/prometheus.py:205-223`，无 Loki 提取器 | 无 | 无（`tasks/V2-009-loki-and-latency.md:48-53`） | 无 | 指标边界部分具备，根因诊断未实现 |
| Post-deployment failure | Git/CI 发布元数据、变更类别与发布前后错误率 | `src/opspilot/evidence/v2.py:200-288,291-342`；Tool 见 `src/opspilot/tools/release.py:349-376` | `PostDeploymentVerifierV2` 固定 PARTIAL，缺运行 revision/SHA 绑定及直接因果（`src/opspilot/diagnosis/release_v2.py:145-196`） | `post_release_rise` SQLite Replay；其他反证仅单测（`tasks/V2-010-git-cicd-post-deployment.md:56-64`） | 无 Git/CI Live | 相关性可审计，故障归因未完成 |

## 阶段取舍

1. 保留 V2-003 已验证的多轮 Runtime、版本化 Case/Replay、只读 Tool 与 Evidence 能力，作为下一阶段 Trace 查询、Budget 管理和离线演示的基础。优先做这些 Runtime 工程能力；详见 `tasks/V3-001-trace-query.md`、`V3-002-budget-manager.md`、`V3-003-offline-demo.md`。
2. 五个优先诊断方向保持在长期范围内，但当前不并行扩张。Readiness、ImagePull 与 Latency 暂缓；独立 Liveness 规则仍在五个方向中，待 Runtime 工程工作完成后按工作单实施。**暂缓不是完成或取消**；保留已有代码、未勾选的验收条件和反证要求。V2-006 的状态与本页同步为 Paused。V2-010 仍为 In Progress，其发布相关性不得提升为受支持的变更根因。V2-012 阶段总验收保持 Planned。
3. 后续恢复某一类时，先补其专用 Verifier、正反/缺证据 Case、真实数据源边界，再更新本表。`COMPLETED` 只能指精确、证据支持的子因；单纯分类、Tool 可用或时间相关性均不计。

## 未同步版本差异

- 协作讨论提及“已有 5/8 类完成”、Prompt ADR 0006 和另一新分支；本次核对的 `origin/main` (`903b8c9`) 没有**所述 Prompt ADR 0006**、五类完成的测试证据或该分支引用。当前本地若以 ADR 0006 编号记录 Trace 决策，那是不同主题，不能作为所述 Prompt 决策的证据。取得确切 commit、PR 或分支引用后应重新审计，不把口头版本信息写为当前仓库事实。
