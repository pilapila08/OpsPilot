# Development Environment

## Toolchain

- Python: `>=3.12,<3.14`
- Dependency management: standard `venv + pip`
- Project metadata and dependencies: `pyproject.toml`
- Tests: pytest
- Static type checking: mypy strict mode
- Validation and serialization: Pydantic v2

选择标准 `venv + pip` 是为了让 V0 在当前环境直接运行，不依赖额外的包管理工具。进入需要锁定完整传递依赖的阶段时，再评估引入锁文件工具，并通过 ADR 记录变更。

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Checks

```powershell
python -m pytest
python -m mypy src tests
```

