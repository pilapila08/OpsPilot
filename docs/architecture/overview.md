# 系统总览

## 定位

OpsPilot 是一个面向 Kubernetes 和微服务故障的诊断 Agent Runtime，而不是开放式运维聊天机器人。系统负责把自然语言问题转换为受控诊断步骤，并用可追溯证据支持最终结论。

## 核心链路

```text
User Query
   -> Intent Router
   -> Planner
   -> Plan Validator
   -> Executor
   -> Tool Gateway
   -> Evidence Store
   -> Verifier
   -> Root Cause + Evidence + Recommendation
```

## 模块职责

| 模块 | 职责 | 不负责 |
|---|---|---|
| Router | 识别意图、故障类型和目标资源 | 执行诊断 |
| Planner | 根据问题和观察结果提出下一步 | 绕过 Tool Registry |
| Plan Validator | 校验步骤、参数、风险和预算 | 生成业务结论 |
| Executor | 调用 Tool、处理超时和错误、记录 Trace | 自由解释 Tool 输出 |
| Tool Gateway | 以最小权限访问外部系统 | 暴露任意 Shell |
| Evidence Store | 保存来源明确、可引用的证据 | 替代原始数据存储 |
| Verifier | 检查 Claim 是否被 Evidence 支持 | 在证据不足时补造事实 |

## 横向能力

- PostgreSQL：任务、运行、调用、证据、结果、版本和评测数据。
- Redis：会话、短期状态、缓存、限流和分布式锁。
- OpenTelemetry：跨 Router、Planner、Tool 和 Verifier 的 Trace。
- Prometheus/Grafana：成功率、延迟、成本、Tool 错误和策略拒绝指标。

## V1 最小闭环

首个闭环只处理 `CrashLoopBackOff + Liveness Probe 配置过早`：

```text
Question -> Pod Status -> Events -> Previous Logs -> Deployment
         -> Evidence -> Verifier -> Diagnosis
```

V1 不包含写操作、RAG、MCP、Multi-Agent 或完整前端。

