from typing import Any

from app.agent.providers.base import ModelCapabilities
from app.agent.providers.openai_compatible import OpenAICompatibleProvider


class QwenProvider(OpenAICompatibleProvider):
    provider_name = "qwen"
    capabilities = ModelCapabilities(supports_multimodal=True, supports_tool_planning=True)

    def _semantic_request_options(self) -> dict[str, Any]:
        # DashScope's direct HTTP API expects this provider-specific option at
        # the top level. Semantic parsing should not spend latency on reasoning.
        return {"enable_thinking": False}
