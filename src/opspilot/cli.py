"""Local, read-only operational commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError

from opspilot.tracing.query import TraceDatabaseUnavailable, TraceView, query_trace


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opspilot")
    commands = parser.add_subparsers(dest="command", required=True)
    trace_parser = commands.add_parser("trace", help="read a persisted diagnosis trace")
    trace_parser.add_argument("trace_id")
    trace_parser.add_argument("--format", choices=("text", "json"), default="text")
    trace_parser.add_argument("--json", action="store_true", help="alias for --format json")
    args = parser.parse_args(argv)
    if args.command == "trace":
        database_url = os.environ.get("OPSPILOT_DATABASE_URL", "sqlite:///./opspilot.db")
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
        f"Planning rounds: {stats.planning_rounds}",
    ]
    for round_item in view.rounds:
        lines.append(
            f"  Round {round_item.round_no}: {round_item.action}  "
            f"prompt={round_item.prompt_version_id}  "
            f"calls={','.join(round_item.call_ids) or '-'}"
        )
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
            f"verified={result.verification.supported}",
            f"Root cause: {result.root_cause}",
            f"Recommendation: {result.recommendation}",
            f"Cited evidence: {','.join(result.cited_evidence_ids) or '-'}",
        ))
    return "\n".join(lines)


def _status(success: bool, error_code: str | None) -> str:
    return "OK" if success else f"FAILED({error_code or 'UNKNOWN'})"
