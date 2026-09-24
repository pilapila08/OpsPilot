# Kubernetes Tools V2

V2-004 adds four Risk 0 reads to the V1 five-Tool registry. V1 registry,
single-Pod resolver and frozen output contracts remain unchanged.

| Tool | SDK read | Scope | Bound |
|---|---|---|---|
| `k8s.get_service` | CoreV1 Service get | exact namespace/name | 32 ports, 20 selector pairs |
| `k8s.get_endpoints` | DiscoveryV1 EndpointSlice list | exact namespace and Service label | 20 slices, 100 endpoints total |
| `k8s.get_ingress` | NetworkingV1 Ingress get | exact namespace/name | 100 routes; host SHA-256 only |
| `k8s.get_resource_usage` | AppsV1 Deployment get, CoreV1 Pod list, Metrics API PodMetrics list | exact Deployment; selector derived from its `matchLabels` | 50 Pods, 100 container samples, five-minute freshness |

The Reader makes one bounded list request and reports Kubernetes continuation as
`truncated`; it never silently presents a partial page as complete. Metrics
rejects truncated lists, stale timestamps and foreign Pod identities. Missing
samples remain `missing_pods`, not zero usage. Multi-Pod snapshots are sorted
by name/UID and retain Deployment generation, UID/resourceVersion and each
Pod's template hash; mixed or incomplete rollouts are marked ambiguous, with
no arbitrary single-Pod choice. Endpoint addresses, Ingress hostnames,
annotations, event bodies and raw environment variables do not enter Tool
outputs or Evidence.

Provision a separate read-only V2 ServiceAccount, not the V1 fixture identity:

```text
core/services: get
discovery.k8s.io/endpointslices: list
networking.k8s.io/ingresses: get
apps/deployments: get
core/pods: list
metrics.k8s.io/pods: list
```

Scope the Role to the operator-approved namespace and grant no write, Secret,
exec or port-forward permissions. The Metrics API must be available separately;
missing permissions or API availability produce classified errors, never a
fallback to a broader identity. In-cluster credentials are used only when
explicitly selected; local kubeconfig mode requires an explicit path and
context.

The opt-in smoke `tests/integration/tools/test_v2_kubernetes_live.py` requires
`OPSPILOT_V2_K8S_LIVE=1`, plus explicit `OPSPILOT_V2_KUBECONFIG`,
`OPSPILOT_V2_CONTEXT`, `OPSPILOT_V2_NAMESPACE`, `OPSPILOT_V2_SERVICE`,
`OPSPILOT_V2_INGRESS` and `OPSPILOT_V2_DEPLOYMENT`. It performs reads only
and makes no fixture or namespace changes. The default suite skips it.
