"""Validated process configuration for the local V1 API."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path

from pydantic import Field, model_validator

from opspilot.agent.schemas import BudgetLimits, StrictSchema
from opspilot.integrations.kubernetes.models import (
    KubernetesClientSettings, KubernetesConnectionMode,
)


class ApiSettings(StrictSchema):
    database_url: str = Field(default="sqlite:///./opspilot.db", min_length=1)
    max_concurrent: int = Field(default=1, ge=1, le=16)
    max_pending: int = Field(default=8, ge=0, le=128)
    shutdown_seconds: float = Field(default=10, gt=0, le=120)
    live_enabled: bool = False
    model_name: str | None = None
    model_timeout_seconds: float = Field(default=30, gt=0, le=300)
    kube_mode: KubernetesConnectionMode = KubernetesConnectionMode.IN_CLUSTER
    kubeconfig_path: Path | None = None
    kube_context: str | None = None
    budget: BudgetLimits = Field(default_factory=BudgetLimits)

    @model_validator(mode="after")
    def validate_live(self) -> ApiSettings:
        if self.live_enabled:
            if not self.model_name:
                raise ValueError("live mode requires an explicit model name")
            KubernetesClientSettings(
                mode=self.kube_mode, kubeconfig_path=self.kubeconfig_path,
                context=self.kube_context,
            )
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ApiSettings:
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        names = {
            "database_url": "DATABASE_URL",
            "max_concurrent": "MAX_CONCURRENT",
            "max_pending": "MAX_PENDING",
            "shutdown_seconds": "SHUTDOWN_SECONDS",
            "live_enabled": "LIVE_ENABLED",
            "model_name": "MODEL",
            "model_timeout_seconds": "MODEL_TIMEOUT_SECONDS",
            "kube_mode": "KUBE_MODE",
            "kubeconfig_path": "KUBECONFIG_PATH",
            "kube_context": "KUBE_CONTEXT",
        }
        for field, suffix in names.items():
            raw = env.get(f"OPSPILOT_{suffix}")
            if raw is not None:
                if field in {"max_concurrent", "max_pending"}:
                    values[field] = int(raw)
                elif field in {"shutdown_seconds", "model_timeout_seconds"}:
                    values[field] = float(raw)
                elif field == "live_enabled":
                    if raw.lower() not in {"true", "false", "1", "0"}:
                        raise ValueError("OPSPILOT_LIVE_ENABLED must be true or false")
                    values[field] = raw.lower() in {"true", "1"}
                else:
                    values[field] = raw
        budget_values: dict[str, object] = {
            key: (env[f"OPSPILOT_{name}"] if key == "max_cost_usd"
                  else int(env[f"OPSPILOT_{name}"]))
            for key, name in {
                "max_steps": "MAX_STEPS",
                "max_tool_calls": "MAX_TOOL_CALLS",
                "max_retries": "MAX_RETRIES",
                "max_tokens": "MAX_TOKENS",
                "max_cost_usd": "MAX_COST_USD",
                "timeout_seconds": "MAX_DURATION_SECONDS",
            }.items()
            if f"OPSPILOT_{name}" in env
        }
        values["budget"] = budget_values
        return cls.model_validate_json(json.dumps(values))
