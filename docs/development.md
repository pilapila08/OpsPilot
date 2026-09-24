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

## 三分钟 Offline Demo

在源码 checkout 中按 Setup 完成 editable 安装后，无需模型密钥、Docker 或 Kubernetes：

```powershell
opspilot demo
# 模块入口等价：python -m opspilot demo
```

默认 CrashLoop 场景经过真实 V1 Runtime、只读 Tool Registry、Extractor、Verifier 和 SQLite 审计，返回 COMPLETED。四类 Evidence 是 Pod 重启、Liveness Events、启动日志、Deployment Probe 配置；实际有五条 Evidence，因为保留了两条 Events。V1 为单次规划，输出明确说明没有 V2 round 记录。

```powershell
# 四轮 V2：status -> deployment -> memory -> finish
opspilot demo --scenario oom
# 一步后耗尽，第二轮 Planner 之前停止；Result PARTIAL，退出码 2
opspilot demo --scenario oom --max-steps 1
# 计划需要四步但只允许三步：执行前拦截，零 Evidence 的 PARTIAL
opspilot demo --max-steps 3
```

Demo 默认保存到当前目录的 `var/demo.db`；可用 `--database ./my-demo.db` 指定 SQLite 文件。每次产生新的 Task/Run/Trace ID，可重复运行。它只迁移这个文件，不读取 `OPSPILOT_DATABASE_URL` 或 Live 配置。默认输出包含轮次、每次 LLM 的 Prompt 版本/token/成本/延迟/error、Tool 尝试序号、Evidence 来源、Verification 和 BudgetStop。

CrashLoop 的 CaseModelClient 用量为零；OOM 为每次 24 输入 + 12 输出的固定示例 token，费用及 provider 延迟为零。这些是离线 fixture/scripted 记账，**不代表付费模型性能或账单**；Run 的 elapsed 仍为本地实际耗时。OOM 的观察与状态转换使用 fixture 回放时钟，LLM 尝试与数据库创建时间仍是本次实际时间，不能将两种时间排序为同一 Live 时间线。Demo 用确定性脚本选择工具，不证明模型的泛化能力。

### 查询刚才的 Trace

复制输出的 Trace ID 后：

```powershell
opspilot trace <trace_id> --database ./var/demo.db
opspilot trace <trace_id> --database ./var/demo.db --json
```

`trace` 不指定文件时使用 `OPSPILOT_DATABASE_URL`，未配置则读取 `./opspilot.db`；它只读已有数据库，不运行迁移或创建缺失文件。查询 API 使用的旧库前，应先正常启动 API 或执行 `alembic upgrade head`，升级至 `20260925_0005`。没有新增 HTTP Trace 接口，原因见 [ADR 0006](adr/0006-local-trace-query.md)。`--json` 的 stdout 是 Trace JSON；Demo 的说明与迁移日志去 stderr。

预算参数为 `--max-steps`、`--max-tool-calls`、`--max-retries`、`--max-tokens`、`--max-cost-usd`、`--timeout-seconds`。除 retries 可为零，其余须为正数。Demo 退出码：0 = COMPLETED，2 = 有 PARTIAL 或参数无效，1 = 运行/存储失败；Trace 退出码：0 = 查到，1 = 未找到，2 = 库不可用或参数无效。

### 三个演示问题

| 问题 | 演示入口 | 可证实的回答与边界 |
|---|---|---|
| 成本怎么控 | OOM `--max-steps 1`，然后查 BudgetStop | 六维统一准入、Planner 前拦截、记录维度/阶段/步骤/用量并产出 Partial。token/cost 按实际返回 usage 结算，一次调用可能跨限，不宣称精确账单预留；见 ADR 0007 |
| 模型挂了怎么办 | 对失败 Run 使用 `trace --json`；回归测试在 `tests/integration/runtime/test_offline_e2e.py` | 失败尝试保留 error_code、latency、usage；Schema 重生成有上限，Tool 重试服从 Taxonomy；provider 原文不进入 Trace。当前没有对所有瞬态模型错误统一自动重试 |
| 改 Prompt 怎么验证 | Trace 的 component / prompt_version_id | 仓储拒绝同 component/version 改内容；SHA-256 和不可原地改写有测试。当前没有所述 Prompt ADR 0006、全套固定文件哈希守卫或 v3 Prompt 分配，不应口头声称已有；具体基线差异见交接文档 |

安装方式目前要求源码 checkout 的 editable 模式，因为 Demo 读取仓库内 fixtures、prompts 和 migrations；不声明已支持单独分发 wheel 的自包含资源包。

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
