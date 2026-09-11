from dataclasses import dataclass
from typing import Literal

from app.agent.providers import DeepSeekProvider, ModelProvider, QwenProvider, ZhipuProvider
from app.core.config import settings
from app.schemas.agent import ModelInput


ModelRoute = Literal["DETERMINISTIC_FAST_PATH", "QWEN_SEMANTIC", "DEEPSEEK_REASONING", "CLARIFICATION"]
ProviderExecutionMode = Literal["AUTO", "QWEN_ONLY", "PRIMARY_ONLY"]


@dataclass(frozen=True)
class RouteDecision:
    route: ModelRoute
    reason: str


@dataclass(frozen=True)
class ProviderExecutionPolicy:
    """Trusted per-request constraint; it may narrow but never add providers."""

    mode: ProviderExecutionMode = "AUTO"

    def allowed_names(self, primary_name: str) -> frozenset[str] | None:
        if self.mode == "AUTO":
            return None
        if self.mode == "QWEN_ONLY":
            return frozenset({"qwen"})
        return frozenset({primary_name})

    def allows(self, provider_name: str, primary_name: str) -> bool:
        allowed = self.allowed_names(primary_name)
        return allowed is None or provider_name in allowed


class ModelRouter:
    """Selects model responsibility; it never decides enterprise facts."""

    def __init__(self, providers: list[ModelProvider], *, primary_name: str, reasoning_name: str, data_mode: str = "disabled"):
        self.providers = {provider.provider_name: provider for provider in providers}
        self.primary_name = primary_name
        self.reasoning_name = reasoning_name
        self.data_mode = data_mode

    def provider(self, name: str) -> ModelProvider | None:
        return self.providers.get(name)

    def semantic_candidates(
        self,
        *,
        has_multimodal_input: bool = False,
        policy: ProviderExecutionPolicy | None = None,
    ) -> list[ModelProvider]:
        """Return the primary plus at most one fallback; never create a retry loop."""
        execution_policy = policy or ProviderExecutionPolicy()
        allowed_names = execution_policy.allowed_names(self.primary_name)
        if allowed_names is not None:
            result = []
            for name in allowed_names:
                provider = self.provider(name)
                if provider and provider.configured and provider.capabilities.supports_tool_planning and (
                    not has_multimodal_input or provider.capabilities.supports_multimodal
                ):
                    result.append(provider)
            return result[:1]
        primary = self.provider(self.primary_name)
        result = []
        if primary and primary.configured and primary.capabilities.supports_tool_planning and (
            not has_multimodal_input or primary.capabilities.supports_multimodal
        ):
            result.append(primary)
        if has_multimodal_input:
            return result
        fallback = self.provider(self.reasoning_name)
        if not fallback or not fallback.configured or not fallback.capabilities.supports_tool_planning:
            fallback = self.provider("zhipu")
        if fallback and fallback.configured and fallback.capabilities.supports_tool_planning and fallback not in result:
            result.append(fallback)
        return result[:2]

    def semantic_route(
        self,
        understanding,
        *,
        has_multimodal_input: bool = False,
        policy: ProviderExecutionPolicy | None = None,
    ) -> RouteDecision:
        if understanding.route == "DETERMINISTIC_FAST_PATH":
            return RouteDecision("DETERMINISTIC_FAST_PATH", "high_confidence_rule")
        if understanding.route == "CLARIFICATION":
            return RouteDecision("CLARIFICATION", "deterministic_missing_required_information")
        candidates = self.semantic_candidates(has_multimodal_input=has_multimodal_input, policy=policy)
        return RouteDecision("QWEN_SEMANTIC", "semantic_or_context_required") if candidates else RouteDecision("CLARIFICATION", "semantic_provider_unavailable")

    def should_reason(self, understanding, *, product_count: int = 0) -> bool:
        if not understanding.requires_reasoning:
            return False
        if not self.provider(self.reasoning_name) or not self.provider(self.reasoning_name).configured:
            return False
        complex_decision = understanding.intent == "selection_recommendation" and (
            product_count >= 3 or bool(understanding.preference_order) or bool(understanding.constraints)
        )
        complex_explanation = understanding.intent == "decision_explanation" and product_count >= 2
        return complex_decision or complex_explanation

    def reasoning_data_allowed(self, products: list[dict]) -> bool:
        if self.data_mode == "disabled":
            return False
        if self.data_mode == "mock_only":
            return bool(products) and all(bool(item.get("is_mock")) for item in products)
        return self.data_mode == "anonymized"

    def status(self) -> dict:
        return {
            "primary": self.primary_name,
            "reasoning": self.reasoning_name,
            "external_reasoning_data_mode": self.data_mode,
            "providers": [provider.status() for provider in self.providers.values()],
            "capability_matrix": {provider.provider_name: provider.capabilities.__dict__ for provider in self.providers.values()},
        }

    def route_input(self, model_input: ModelInput) -> RouteDecision:
        candidates = self.semantic_candidates(has_multimodal_input=model_input.has_multimodal_input)
        if not candidates:
            return RouteDecision("CLARIFICATION", "compatible_provider_not_configured")
        return RouteDecision("QWEN_SEMANTIC", f"compatible_provider:{candidates[0].provider_name}")


def build_model_router() -> ModelRouter:
    qwen_key = settings.qwen_api_key or settings.dashscope_api_key
    providers: list[ModelProvider] = [
        QwenProvider(api_key=qwen_key, base_url=settings.qwen_base_url, model_name=settings.primary_llm_model, timeout_seconds=settings.provider_timeout_seconds),
        DeepSeekProvider(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, model_name=settings.reasoning_llm_model, timeout_seconds=settings.provider_timeout_seconds),
        ZhipuProvider(api_key=settings.zhipu_api_key, base_url=settings.zhipu_base_url, model_name=settings.zhipu_model, timeout_seconds=settings.zhipu_timeout_seconds),
    ]
    return ModelRouter(
        providers,
        primary_name=settings.primary_llm_provider.casefold(),
        reasoning_name=settings.reasoning_llm_provider.casefold(),
        data_mode=settings.external_reasoning_data_mode.casefold(),
    )


def semantic_context_slots(packet: dict) -> dict:
    """Remove tenant/session/product identities before any external semantic call."""
    task = dict(packet.get("task_context") or {})
    entity_count = len(task.pop("active_entities_display_names", []) or [])
    task["active_entity_slots"] = [f"T{index + 1}" for index in range(entity_count)]
    return {
        "available_sources": [
            source for source, key in (
                ("ui_selection", "current_selected_product_ids"),
                ("last_explicit_entity", "last_explicit_product_ids"),
                ("last_resolved_entity", "last_resolved_product_ids"),
                ("last_comparison", "last_comparison_order"),
            ) if packet.get(key)
        ],
        "last_intent": packet.get("last_intent"),
        "last_metric": packet.get("last_metric"),
        "last_requested_dimensions": packet.get("last_requested_dimensions") or [],
        "last_preference_order": packet.get("last_preference_order") or [],
        "last_negative_scope": packet.get("last_negative_scope") or [],
        "reference_candidates": packet.get("reference_candidates") or [],
        "comparison_slot_count": len(packet.get("last_comparison_order") or []),
        "comparison_slots": [f"P{index + 1}" for index, _ in enumerate(packet.get("last_comparison_order") or [])],
        "ordinal_slots": [f"P{index + 1}" for index, _ in enumerate(packet.get("last_comparison_order") or [])],
        "task_context": task,
        "allowed_tools": packet.get("allowed_tools") or [],
    }


def redact_external_query(query: str, products) -> tuple[str, dict[str, str]]:
    """Replace known catalogue identifiers before a query crosses the provider boundary."""
    safe_query = query
    placeholders: dict[str, str] = {}
    seen: set[str] = set()
    identifiers = []
    for product in products:
        for value in (product.id, product.external_product_id, product.sku):
            if value and value not in seen and value in safe_query:
                seen.add(value)
                identifiers.append(value)
    for index, value in enumerate(sorted(identifiers, key=len, reverse=True), start=1):
        placeholder = f"[PRODUCT_REF_{index}]"
        safe_query = safe_query.replace(value, placeholder)
        placeholders[placeholder] = value
    return safe_query, placeholders
