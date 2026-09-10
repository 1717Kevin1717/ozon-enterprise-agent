from app.agent.providers.base import ModelCapabilities
from app.agent.providers.openai_compatible import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    provider_name = "deepseek"
    capabilities = ModelCapabilities(supports_reasoning=True, supports_tool_planning=True)
