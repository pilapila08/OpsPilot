# OpsPilot

OpsPilot 是面向 Kubernetes 和微服务故障诊断的可审计 Agent Runtime。当前项目处于 V0 协议设计阶段。

## 开发环境

需要 Python 3.12 或 3.13，依赖使用标准 `venv + pip` 管理。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 验证

```powershell
python -m pytest
python -m mypy src tests
```

开发前从 [AGENTS.md](AGENTS.md)、[项目状态](docs/STATUS.md)和[当前任务](tasks/README.md)恢复上下文。

