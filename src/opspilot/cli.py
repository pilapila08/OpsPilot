"""Local audit queries and deterministic offline demonstrations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import URL

from opspilot.agent.schemas import BudgetLimits
from opspilot.agent.state import AgentStatus
from opspilot.storage.contracts import RuntimePersistenceError
from opspilot.tracing.query import TraceDatabaseUnavailable, TraceView, query_trace


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opspilot")
    commands = parser.add_subparsers(dest="command", required=True)
    trace_parser = commands.add_parser("trace", help="read a persisted diagnosis trace")
    trace_parser.add_argument("trace_id")
    trace_parser.add_argument("--format", choices=("text", "json"), default="text")
    trace_parser.add_argument("--json", action="store_true", help="alias for --format json")
    trace_parser.add_argument("--database", type=Path, help="query this local SQLite file")
    demo_parser = commands.add_parser("demo", help="run a Case offline and print its persisted trace")
    demo_parser.add_argument("--scenario", choices=("crashloop", "oom"), default="crashloop")
    demo_parser.add_argument("--database", type=Path, default=Path("var/demo.db"))
    demo_parser.add_argument("--json", action="store_true")
    for option in ("steps", "tool-calls", "retries", "tokens"):
        demo_parser.add_argument(f"--max-{option}", type=int)
    demo_parser.add_argument("--max-cost-usd")
    demo_parser.add_argument("--timeout-seconds", type=int)
    args = parser.parse_args(argv)
    if args.command == "demo":
        from opspilot.runtime.demo import run_demo

        limits = {key: getattr(args, key) for key in (
            "max_steps", "max_tool_calls", "max_retries", "max_tokens",
            "max_cost_usd", "timeout_seconds",
        ) if getattr(args, key) is not None}
        try:
            budget = BudgetLimits.model_validate_json(json.dumps(limits))
        except ValueError:
            parser.error("invalid demo budget limits; use positive values (retries may be zero)")
        try:
            demo_view = asyncio.run(run_demo(
                database_path=args.database, scenario=args.scenario, budget_limits=budget,
            ))
        except (OSError, ValueError, SQLAlchemyError, RuntimePersistenceError, CommandError):
            print("Offline demo could not load its Case or persist its audit database.", file=sys.stderr)
            return 1
        notice = (
            "Offline Replay: no remote model or cluster. Usage is fixture/scripted data, not billed inference.\n"
            f"Database: {args.database.resolve()}"
        )
        print(notice, file=sys.stderr if args.json else sys.stdout)
        print(json.dumps(demo_view.model_dump(mode="json"), ensure_ascii=False, indent=2)
              if args.json else format_trace(demo_view))
        if demo_view.run.status is AgentStatus.COMPLETED:
            return 0
        return 2 if demo_view.result is not None and demo_view.result.status == "PARTIAL" else 1
    if args.command == "trace":
        database_url = (
            URL.create("sqlite", database=str(args.database.resolve())).render_as_string(hide_password=False)
            if args.database else os.environ.get("OPSPILOT_DATABASE_URL", "sqlite:///./opspilot.db")
        )
        try:
            view = query_trace(database_url, args.trace_id)
        except (TraceDatabaseUnavailable, SQLAlchemyError, ValueError, OSError):
            print("Trace database is unavailable or invalid.", file=sys.stderr)
            return 2
        if view is None:
            print(f"Trace not found: {args.trace_id}", file=sys.stderr)
            return 1
        if args.json or args.format == "json":
            print(json.dumps(view.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            print(format_trace(view))
        return 0
    return 2


def format_trace(view: TraceView) -> str:
    run = view.run
    stats = view.statistics
    lines = [
        f"Trace: {run.trace_id}",
        f"Run: {run.run_id}  Task: {run.task_id}  Attempt: {run.attempt_no}",
        f"Runtime: {run.runtime_version} ({run.planning_mode})  Status: {run.status.value}",
        f"Started: {run.started_at.isoformat()}",
        f"Planning rounds: {stats.planning_rounds}"
        + (" (V1 single-pass plan; no V2 round records)" if run.runtime_version == "v1" else ""),
    ]
    for round_item in view.rounds:
        lines.append(
            f"  Round {round_item.round_no}: {round_item.action}  "
            f"prompt={round_item.prompt_version_id}  "
            f"calls={','.join(round_item.call_ids) or '-'}"
        )
    lines.append(f"Budget stops: {stats.budget_stops}")
    for stop in view.budget_stops:
        reason = stop.reason
        used = reason.budget
        limits = used.limits
        lines.extend((
            f"  Stop: dimension={reason.dimension}  phase={reason.phase}  "
            f"step={reason.step_no}  round={stop.round_no if stop.round_no is not None else '-'}  "
            f"kind={reason.kind}  requested={reason.requested}  "
            f"recorded={stop.recorded_at.isoformat()}",
            f"    steps={used.steps_used}/{limits.max_steps}  "
            f"tool_calls={used.tool_calls_used}/{limits.max_tool_calls}  "
            f"retries={used.retries_used}/{limits.max_retries}  "
            f"tokens={used.tokens_used}/{limits.max_tokens}  "
            f"cost_usd={used.cost_usd}/{limits.max_cost_usd}  "
            f"elapsed_s={used.elapsed_seconds}/{limits.timeout_seconds}",
        ))
    lines.append(
        f"Model attempts: {stats.model_attempts}  Failed: {stats.failed_model_attempts}  "
        f"Tokens: {stats.input_tokens}+{stats.output_tokens}  "
        f"Cost USD: {stats.model_cost_usd}"
    )
    for call in view.model_calls:
        lines.append(
            f"  LLM {call.sequence_no}: {call.component}  prompt={call.prompt_version_id}  "
            f"round={call.round_no if call.round_no is not None else '-'}  "
            f"model={call.provider}/{call.model_name}  "
            f"tokens={call.input_tokens}/{call.output_tokens}/"
            f"{call.input_tokens + call.output_tokens}  "
            f"cost_usd={call.cost_usd}  latency_ms={call.latency_ms}  "
            f"retry_count={call.retry_count}  "
            f"status={_status(call.success, call.error_code.value if call.error_code else None)}"
        )
    lines.append(
        f"Tool attempts: {stats.tool_attempts}  Failed: {stats.failed_tool_attempts}  "
        f"Retries: {stats.tool_retries}"
    )
    for attempt in view.tool_attempts:
        lines.append(
            f"  Tool {attempt.sequence_no}: {attempt.tool_name}  "
            f"call={attempt.logical_call_id}  attempt={attempt.attempt_no}  "
            f"status={_status(attempt.success, attempt.error_code.value if attempt.error_code else None)}"
        )
    lines.append(f"Evidence: {stats.evidence_count}")
    for evidence in view.evidence:
        lines.append(
            f"  {evidence.evidence_id}: {evidence.source}  "
            f"resource={evidence.resource}  tool_attempt={evidence.tool_call_id}"
        )
    if view.result is None:
        lines.append("Result: none")
    else:
        result = view.result
        lines.extend((
            f"Result: {result.status}  confidence={result.confidence}  "
            f"verified={result.verification.supported}  "
            f"verification_kind={result.verification.kind or 'evidence'}",
            f"Root cause: {result.root_cause}",
            f"Recommendation: {result.recommendation}",
            f"Cited evidence: {','.join(result.cited_evidence_ids) or '-'}",
            f"Verification: supported={result.verification.supported}  "
            f"checked={len(result.verification.checked_evidence_ids)}  "
            f"missing={result.verification.missing_evidence_count}  "
            f"contradictions={result.verification.contradiction_count}",
        ))
    return "\n".join(lines)


def _status(success: bool, error_code: str | None) -> str:
    return "OK" if success else f"FAILED({error_code or 'UNKNOWN'})"
