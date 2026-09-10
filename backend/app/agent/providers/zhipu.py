from app.agent.providers.base import ModelCapabilities
from app.agent.providers.openai_compatible import OpenAICompatibleProvider


class ZhipuProvider(OpenAICompatibleProvider):
    provider_name = "zhipu"
    use_json_response_format = False
    capabilities = ModelCapabilities(supports_tool_planning=True)
