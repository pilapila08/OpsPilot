import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from opspilot.cases import CaseDefinition

PROJECT_ROOT = Path(__file__).parents[3]
CASE_FILE = (
    PROJECT_ROOT / "fixtures" / "cases" / "crashloop-liveness-v1" / "case.json"
)


def _case_payload() -> dict[str, object]:
    return cast(dict[str, object], json.loads(CASE_FILE.read_text(encoding="utf-8")))


def test_case_rejects_fixture_path_escape() -> None:
    payload = _case_payload()
    payload["broken_manifest"] = "../outside.json"

    with pytest.raises(
        ValidationError,
        match="fixture paths must stay inside the case directory",
    ):
        CaseDefinition.model_validate_json(json.dumps(payload), strict=True)


def test_case_requires_all_crashloop_signals() -> None:
    payload = _case_payload()
    requirements = cast(list[dict[str, object]], payload["evidence_requirements"])
    payload["evidence_requirements"] = requirements[:-1]

    with pytest.raises(
        ValidationError,
        match="case must cover every required CrashLoopBackOff signal",
    ):
        CaseDefinition.model_validate_json(json.dumps(payload), strict=True)
