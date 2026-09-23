# Development Environment

## Toolchain

- Python: `>=3.12,<3.14`
- Dependency management: standard `venv + pip`
- Project metadata and dependencies: `pyproject.toml`
- Tests: pytest
- Static type checking: mypy strict mode
- Validation and serialization: Pydantic v2
- ORM and migrations: SQLAlchemy 2.0 + Alembic
- PostgreSQL driver: psycopg 3

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

## Database Migrations

```powershell
$env:OPSPILOT_DATABASE_URL = "postgresql+psycopg://user:password@localhost/opspilot"
alembic upgrade head
alembic downgrade base
```

`OPSPILOT_DATABASE_URL` 覆盖 `alembic.ini` 中的开发占位地址。生产 Schema 以 PostgreSQL 为准；测试套件使用临时 SQLite 验证迁移可逆性，并单独编译 PostgreSQL DDL。

## V1 Diagnosis API

仅限本机开发环境；V1 没有认证或租户隔离，不应暴露到公网。默认 SQLite 路径为当前目录下的 `opspilot.db`，启动时执行 Alembic 升级：

```powershell
python -m uvicorn opspilot.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

```powershell
$body = @{query="slow-start-api 为什么一直重启？"; namespace="opspilot-fixtures"; mode="replay"; case_id="crashloop_liveness_v1"} | ConvertTo-Json
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/diagnosis -ContentType application/json -Body $body
Invoke-RestMethod -Uri "http://127.0.0.1:8000/diagnosis/$($created.task_id)"
```

`POST` 返回 202 和持久化 Task ID；`GET` 返回状态、Trace、证据的安全投影和可用的验证结果。可选 `Idempotency-Key` 请求头为同一请求复用 Task；同一个 key 对不同请求返回 409。并发和排队容量默认分别为 1、8，进程关闭最多等待 10 秒。V1 不恢复崩溃时遗留的 RUNNING Task。

配置变量：`OPSPILOT_DATABASE_URL`、`OPSPILOT_MAX_CONCURRENT`、`OPSPILOT_MAX_PENDING`、`OPSPILOT_SHUTDOWN_SECONDS`、`OPSPILOT_MAX_STEPS`、`OPSPILOT_MAX_TOOL_CALLS`、`OPSPILOT_MAX_RETRIES`、`OPSPILOT_MAX_TOKENS`、`OPSPILOT_MAX_COST_USD` 和 `OPSPILOT_MAX_DURATION_SECONDS`。Replay 不需要模型凭据或 Kubernetes。

## Optional Live Smoke

此路径要求操作者预先准备隔离集群、只读 Service Account、Docker、kind 和 kubectl。Agent 本身不执行写操作。先按 [Case 操作说明](../fixtures/cases/crashloop-liveness-v1/README.md)构建镜像并部署 broken manifest，等待 Pod 进入 CrashLoopBackOff；用操作者权限完成部署，运行 API 的进程只使用下列只读权限：

```text
pods: get, list
pods/log: get
events: list
deployments.apps: get
```

使用独立的、限权 kubeconfig 和明确的 context；不要把路径、Token 或文件内容提交到仓库：

```powershell
$env:OPSPILOT_LIVE_ENABLED = "true"
$env:OPSPILOT_MODEL = "<configured OpenAI model>"
$env:OPENAI_API_KEY = "<local secret>"
$env:OPSPILOT_KUBE_MODE = "kubeconfig"
$env:OPSPILOT_KUBECONFIG_PATH = "<absolute path to read-only kubeconfig>"
$env:OPSPILOT_KUBE_CONTEXT = "<explicit context>"
$env:OPSPILOT_LIVE_SMOKE = "1"
python -m pytest tests/integration/api/test_live_smoke.py -m live -q
```

测试逐一调用五个 Risk 0 Tool，再以 `mode=live` 通过 POST/GET 跑完整 Runtime 并核对四类 Evidence。使用 in-cluster Service Account 时将 `OPSPILOT_KUBE_MODE` 设为 `in_cluster`，不传 kubeconfig/path/context。Live 凭据或 model 缺失会显式拒绝启动/请求，不回退到 Replay。

测试通过后由操作者应用 Case 的 fixed manifest，确认 Deployment Ready 与零重启，再清理 fixture namespace；使用 Case 文档给出的命令。最后在 [V1 验收报告](roadmap/v1-acceptance.md)登记日期、环境、测试结果及修复后检查。默认 CI 不设置 `OPSPILOT_LIVE_SMOKE`，故跳过 Live。
