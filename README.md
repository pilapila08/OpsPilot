# OpsPilot

OpsPilot 是面向 Kubernetes 和微服务故障诊断的可审计 Agent Runtime。V1 单故障闭环已验收，V2 有界多轮 Runtime 与部分离线故障切片已实现，V3 Trace、Budget 和 Offline Demo 已完成。V2 八类故障的阶段总验收尚未完成，详见 [V2 能力范围与计数口径](docs/roadmap/v2-scope.md) 和 [项目状态](docs/STATUS.md)。

## 开发环境

需要 Python 3.12 或 3.13，依赖使用标准 `venv + pip` 管理。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 离线演示

```powershell
opspilot demo
opspilot demo --scenario oom
opspilot demo --scenario oom --max-steps 1
```

完整 Trace 与预算终止可在本地查看，无需模型密钥或集群。详见 [三分钟演示与 CLI](docs/development.md#三分钟-offline-demo)；继续开发前阅读 [V3 交接](docs/handoffs/2026-09-25-v3-runtime.md)。

## 验证

```powershell
python -m pytest
python -m mypy src tests
```

开发前从 [AGENTS.md](AGENTS.md)、[项目状态](docs/STATUS.md)和[当前任务](tasks/README.md)恢复上下文。

