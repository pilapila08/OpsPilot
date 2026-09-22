# CrashLoop Liveness V1

## 故障机制

应用启动后等待 40 秒才监听 `8080`。故障清单在第 10 秒开始 Liveness Probe，每 5 秒检查一次，连续 3 次失败后重启容器：

```text
t=0s   application boot starts
t=10s  liveness failure 1
t=15s  liveness failure 2
t=20s  liveness failure 3 -> restart
t=40s  application would become ready, but this point is never reached
```

修复清单增加 Startup Probe。`periodSeconds=5`、`failureThreshold=10` 提供约 50 秒启动窗口；Startup Probe 成功前，Liveness 不会执行。

## Fixture 内容

- `case.json`：回放顺序、Evidence、预期 Claim、Verification 和恢复条件。
- `responses/fault/`：Pod Status、Events、Previous Logs 和 Deployment 固定响应。
- `responses/recovery/`：修复后的 Ready Pod Status。
- `manifests/`：Namespace、故障 Deployment 和修复 Deployment。
- `app/`：只使用 Python 标准库的慢启动 HTTP 应用及 Dockerfile。

时间戳、Pod 名称和调用 ID 都是固定测试数据，不来自生产环境。

## 离线验证

离线验证不需要 Docker 或 Kubernetes：

```powershell
python -m pytest tests/integration/cases/test_crashloop_case.py
```

测试会加载所有 ToolResponse，计算故障与修复探针时间窗口，检查四类 required Evidence，并验证恢复快照为 `Running`、`Ready`、零重启。

## 本地 Kubernetes 复现

以下命令以 kind 为例。需要本机已有 Docker、kind 和 kubectl：

```powershell
docker build -t opspilot/slow-start-api:v0-005 fixtures/cases/crashloop-liveness-v1/app
kind load docker-image opspilot/slow-start-api:v0-005
kubectl apply -f fixtures/cases/crashloop-liveness-v1/manifests/namespace.json
kubectl apply -f fixtures/cases/crashloop-liveness-v1/manifests/broken-deployment.json
kubectl -n opspilot-fixtures get pods --watch
```

观察故障证据：

```powershell
kubectl -n opspilot-fixtures describe pod -l app.kubernetes.io/name=slow-start-api
kubectl -n opspilot-fixtures logs -l app.kubernetes.io/name=slow-start-api --previous
```

应用修复并等待新 Pod：

```powershell
kubectl apply -f fixtures/cases/crashloop-liveness-v1/manifests/fixed-deployment.json
kubectl -n opspilot-fixtures rollout status deployment/slow-start-api --timeout=120s
kubectl -n opspilot-fixtures get pods
```

清理：

```powershell
kubectl delete -f fixtures/cases/crashloop-liveness-v1/manifests/namespace.json
```

本地集群中的 Pod 名称、IP 和事件时间会变化；Offline Replay 始终使用 `responses/` 中的固定数据。
