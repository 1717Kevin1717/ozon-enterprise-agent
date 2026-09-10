from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelCapabilities:
    supports_text: bool = True
    supports_multimodal: bool = False
    supports_structured_output: bool = True
    supports_reasoning: bool = False
    supports_tool_planning: bool = False


@dataclass(frozen=True)
class ProviderCallResult:
    content: str
    provider_name: str
    model_name: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int | None = None
    upstream_status: int | None = None


class ProviderFailure(RuntimeError):
    """A sanitized provider failure safe to expose through AgentRun metadata."""

    def __init__(
        self,
        code: str,
        provider_name: str,
        *,
        exception_class: str | None = None,
        failure_stage: str | None = None,
        elapsed_ms: int | None = None,
        upstream_status: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.failure_code = code
        self.provider_name = provider_name
        self.exception_class = exception_class
        self.failure_stage = failure_stage
        self.elapsed_ms = elapsed_ms
        self.upstream_status = upstream_status


class ModelProvider(ABC):
    provider_name: str
    model_name: str
    capabilities: ModelCapabilities

    def __init__(self, *, api_key: str, base_url: str, model_name: str, timeout_seconds: int = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model_name)

    def status(self) -> dict[str, Any]:
        state = "configured_unverified" if self.configured else "not_configured"
        if self.provider_name == "zhipu":
            state = "legacy_configured_unverified" if self.configured else "legacy_not_configured"
        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "configured": self.configured,
            "status": state,
            "ready_for_mock": True,
            "real_smoke_verified": False,
            "legacy": self.provider_name == "zhipu",
            "capabilities": self.capabilities.__dict__,
        }

    @abstractmethod
    async def semantic_interpret(self, query: Any, context_slots: dict[str, Any]) -> ProviderCallResult:
        raise NotImplementedError

    @abstractmethod
    async def reason(self, decision_packet: dict[str, Any]) -> ProviderCallResult:
        raise NotImplementedError
