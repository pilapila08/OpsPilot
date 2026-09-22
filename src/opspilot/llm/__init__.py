"""Provider-neutral structured model gateway."""

from opspilot.llm.client import ScriptedModelClient, StructuredModelClient
from opspilot.llm.audit import (
    InMemoryModelAuditRepository,
    ModelAuditError,
    ModelAuditRepository,
    ModelCallAttempt,
    RecordedModelCall,
)
from opspilot.llm.errors import (
    ModelBudgetError,
    ModelConfigurationError,
    ModelContextError,
    ModelExternalError,
    ModelGatewayError,
    ModelPermissionError,
    ModelRateLimitError,
    ModelSchemaError,
    ModelTimeoutError,
    RouterScopeError,
)
from opspilot.llm.models import (
    ModelMessage,
    ModelResponseMetadata,
    ModelRole,
    ModelUsage,
    PromptReference,
    PromptTemplate,
    ScriptedModelResponse,
    StructuredModelConfig,
    StructuredModelRequest,
    StructuredModelResult,
)
from opspilot.llm.prompts import load_prompt
from opspilot.llm.openai_adapter import (
    OpenAIAdapterSettings,
    OpenAIStructuredModelClient,
    build_openai_structured_client,
)

__all__ = [
    "ModelBudgetError",
    "InMemoryModelAuditRepository",
    "ModelAuditError",
    "ModelAuditRepository",
    "ModelCallAttempt",
    "ModelConfigurationError",
    "ModelContextError",
    "ModelExternalError",
    "ModelGatewayError",
    "ModelMessage",
    "ModelPermissionError",
    "ModelRateLimitError",
    "ModelResponseMetadata",
    "ModelRole",
    "ModelSchemaError",
    "ModelTimeoutError",
    "ModelUsage",
    "OpenAIAdapterSettings",
    "OpenAIStructuredModelClient",
    "PromptReference",
    "PromptTemplate",
    "RecordedModelCall",
    "RouterScopeError",
    "ScriptedModelClient",
    "ScriptedModelResponse",
    "StructuredModelClient",
    "StructuredModelConfig",
    "StructuredModelRequest",
    "StructuredModelResult",
    "build_openai_structured_client",
    "load_prompt",
]
