"""The demo is an offline Runtime consumer whose persisted Trace can be queried again."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from opspilot.cli import main


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the offline demo tried to connect to a network")

    # Windows asyncio uses a loopback socketpair for its own wakeup pipe.
    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("OPSPILOT_LIVE_ENABLED", "true")
    other = tmp_path / "must-not-be-migrated.db"
    monkeypatch.setenv("OPSPILOT_DATABASE_URL", f"sqlite:///{other.as_posix()}")
    yield
    assert not other.exists()


def test_default_demo_completes_four_evidence_and_can_be_queried_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "demo.db"
    args = ["demo", "--database", str(path), "--json"]
    assert main(args) == 0
    output = capsys.readouterr()
    first = json.loads(output.out)
    assert "not billed inference" in output.err
    assert first["run"]["status"] == "COMPLETED"
    assert first["run"]["planning_mode"] == "single-pass"
    assert first["statistics"]["model_attempts"] == 3
    assert first["statistics"]["tool_attempts"] == 4
    assert {item["source"] for item in first["evidence"]} == {
        "kubernetes_status", "kubernetes_events", "kubernetes_logs", "kubernetes_deployment",
    }
    assert first["result"]["verification"]["supported"] is True
    # Four signal sources produce five rows because two Events are retained.
    assert len(first["result"]["verification"]["checked_evidence_ids"]) == 5
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["run"]["trace_id"] != first["run"]["trace_id"]
    assert main(["trace", first["run"]["trace_id"], "--database", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert not (tmp_path / "must-not-be-migrated.db").exists()


def test_oom_demo_exposes_real_planning_rounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["demo", "--scenario", "oom", "--database", str(tmp_path / "oom.db"), "--json"]) == 0
    trace = json.loads(capsys.readouterr().out)
    assert [item["round_no"] for item in trace["rounds"]] == [1, 2, 3, 4]
    assert trace["statistics"]["model_attempts"] == 5
    assert trace["statistics"]["tool_attempts"] == 3
    assert trace["result"]["verification"]["supported"] is True
    assert trace["rounds"][0]["evidence_ids"] == []
    assert trace["rounds"][1]["evidence_ids"]


@pytest.mark.parametrize(("scenario", "steps", "phase", "calls", "evidence"), [
    ("crashloop", "3", "plan.admission", 0, 0),
    ("oom", "1", "planner.before", 1, 2),
])
def test_demo_budget_partial_is_persisted_without_extra_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], scenario: str,
    steps: str, phase: str, calls: int, evidence: int,
) -> None:
    assert main([
        "demo", "--scenario", scenario, "--database", str(tmp_path / "limited.db"),
        "--max-steps", steps, "--json",
    ]) == 2
    trace = json.loads(capsys.readouterr().out)
    assert trace["run"]["status"] == "BUDGET_EXCEEDED"
    assert trace["result"]["status"] == "PARTIAL"
    assert trace["result"]["schema_version"] == 3
    assert trace["result"]["claim_ids"] == []
    assert trace["statistics"]["model_attempts"] == 2
    assert trace["statistics"]["tool_attempts"] == calls
    assert trace["statistics"]["evidence_count"] == evidence
    assert trace["budget_stops"][0]["reason"]["dimension"] == "steps"
    assert trace["budget_stops"][0]["reason"]["phase"] == phase


def test_demo_text_and_invalid_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["demo", "--database", str(tmp_path / "text.db")]) == 0
    text = capsys.readouterr().out
    for marker in (
        "Offline Replay", "not billed inference", "V1 single-pass", "prompt=",
        "tokens=", "cost_usd=", "latency_ms=", "attempt=1", "Evidence: 5",
        "Verification: supported=True",
    ):
        assert marker in text
    invalid = tmp_path / "invalid.db"
    with pytest.raises(SystemExit) as exc:
        main(["demo", "--database", str(invalid), "--timeout-seconds", "0"])
    assert exc.value.code == 2
    assert not invalid.exists()
