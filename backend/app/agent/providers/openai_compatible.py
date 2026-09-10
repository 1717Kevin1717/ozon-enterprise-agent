import json
import ssl
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.agent.providers.base import ModelProvider, ProviderCallResult, ProviderFailure
from app.schemas.agent import GroundedReasoningOutput, ModelInput, QueryUnderstanding
from app.schemas.semantic_vocabulary import semantic_output_contract


@dataclass(frozen=True)
class FailureClassification:
    code: str
    stage: str
    upstream_status: int | None = None


def _caused_by_tls(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _classify_http_error(exc: Exception) -> FailureClassification:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return FailureClassification("AUTH_FAILED", "RESPONSE_READ", status)
        if status == 404:
            return FailureClassification("MODEL_NOT_FOUND", "RESPONSE_READ", status)
        if status == 429:
            return FailureClassification("RATE_LIMITED", "RESPONSE_READ", status)
        if status >= 500:
            return FailureClassification("PROVIDER_INTERNAL_ERROR", "RESPONSE_READ", status)
        return FailureClassification("HTTP_ERROR", "RESPONSE_READ", status)
    if isinstance(exc, httpx.ConnectTimeout):
        return FailureClassification("NETWORK_TIMEOUT", "DNS_CONNECT")
    if isinstance(exc, httpx.ReadTimeout):
        return FailureClassification("NETWORK_TIMEOUT", "RESPONSE_WAIT")
    if isinstance(exc, httpx.WriteTimeout):
        return FailureClassification("NETWORK_TIMEOUT", "REQUEST_SEND")
    if isinstance(exc, httpx.PoolTimeout):
        return FailureClassification("NETWORK_TIMEOUT", "REQUEST_SEND")
    if isinstance(exc, httpx.ProxyError):
        return FailureClassification("PROXY_ERROR", "REQUEST_SEND")
    if isinstance(exc, httpx.ConnectError):
        if _caused_by_tls(exc):
            return FailureClassification("TLS_ERROR", "TLS_CONNECT")
        return FailureClassification("CONNECT_ERROR", "DNS_CONNECT")
    if isinstance(exc, httpx.RemoteProtocolError):
        return FailureClassification("REMOTE_PROTOCOL_ERROR", "RESPONSE_READ")
    if isinstance(exc, httpx.ReadError):
        return FailureClassification("RESPONSE_READ_ERROR", "RESPONSE_READ")
    if isinstance(exc, httpx.DecodingError):
        return FailureClassification("INVALID_RESPONSE", "RESPONSE_PARSE")
    if isinstance(exc, (httpx.WriteError, httpx.TooManyRedirects, httpx.RequestError)):
        return FailureClassification("HTTP_ERROR", "REQUEST_SEND")
    if isinstance(exc, TimeoutError):
        return FailureClassification("NETWORK_TIMEOUT", "RESPONSE_WAIT")
    return FailureClassification("PROVIDER_INTERNAL_ERROR", "REQUEST_SEND")


def _error_code(exc: Exception) -> str:
    """Compatibility helper retained for callers that need only the safe code."""
    return _classify_http_error(exc).code


def extract_message_content(payload: dict[str, Any]) -> str:
    """Normalize supported OpenAI-compatible content envelopes.

    Transport compatibility stops here: the returned text must still pass the
    strict QueryUnderstanding schema and semantic guard. Empty, malformed, or
    non-textual responses remain INVALID_RESPONSE.
    """
    message = payload["choices"][0]["message"]
    content = message["content"]
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, dict):
        text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                raise ValueError("unsupported content block")
            value = block.get("text", block.get("content"))
            if isinstance(value, dict):
                value = value.get("value")
            if not isinstance(value, str):
                raise ValueError("content block has no text")
            parts.append(value)
        text = "".join(parts).strip()
    else:
        raise ValueError("unsupported message content")
    if not text:
        raise ValueError("empty model content")
    return text


class OpenAICompatibleProvider(ModelProvider):
    use_json_response_format = True
    semantic_max_tokens = 700
    trust_environment = True
    semantic_prompt = (
        "你是企业 Agent 的语义解释器，只输出 JSON SemanticFrame，不回答业务事实。"
        "entity_mentions只放当前query中可独立解析的显式商品名、简称或商品标识；"
        "代词、序数、指标、维度和上一结果引用必须放入references/reference_slots/metrics，不得放入entity_mentions。"
        "references只能放当前query中逐字出现的指代表达；隐式省略主语时references留空，只设置requires_context和reference_slots。"
        "上下文商品只使用reference_slots，不猜测内部ID。"
        "识别 intent、requested_dimensions、metrics、constraints、negative_scope、preference_order。"
        "intent 是任务类型，requested_dimensions 是业务分析领域，metrics 是领域内的具体指标或分析模式。"
        "planned_tools只是建议，可以为空；后端会根据规范化intent、维度和metric生成并授权最终工具计划。"
        "严格使用随后 semantic_value_contract 中的 canonical values；不创造新维度。"
        "省略商品但存在可用上下文时设置 requires_context=true。"
        "只有仍缺必要信息时设置 clarification_required=true。"
        "不得生成价格、利润、销量、合规、风险或推荐结论。"
    )
    reasoning_prompt = (
        "你只解释后端已验证的匿名决策事实。不得新增数字、商品身份、合规状态或推荐结论，"
        "不得把 BLOCKED 商品改为正式推荐。仅输出给定 JSON Schema。"
    )

    def _semantic_request_options(self) -> dict[str, Any]:
        return {}

    def _request_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _request_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _build_request_body(
        self,
        *,
        messages: list[dict[str, Any]],
        max_tokens: int,
        request_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if self.use_json_response_format:
            body["response_format"] = {"type": "json_object"}
        if request_options:
            body.update(request_options)
        return body

    def _failure(
        self,
        classification: FailureClassification,
        exc: Exception,
        started: float,
    ) -> ProviderFailure:
        return ProviderFailure(
            classification.code,
            self.provider_name,
            exception_class=type(exc).__name__,
            failure_stage=classification.stage,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            upstream_status=classification.upstream_status,
        )

    async def _chat(
        self,
        *,
        messages: list[dict[str, Any]],
        max_tokens: int,
        request_options: dict[str, Any] | None = None,
    ) -> ProviderCallResult:
        if not self.configured:
            raise ProviderFailure("KEY_MISSING", self.provider_name)
        started = time.perf_counter()
        try:
            body = self._build_request_body(
                messages=messages, max_tokens=max_tokens, request_options=request_options,
            )
        except Exception as exc:
            classification = FailureClassification("PROVIDER_INTERNAL_ERROR", "REQUEST_BUILD")
            raise self._failure(classification, exc, started) from exc
        try:
            json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError) as exc:
            classification = FailureClassification("REQUEST_SERIALIZATION_ERROR", "SERIALIZATION")
            raise self._failure(classification, exc, started) from exc
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=self.trust_environment) as client:
                response = await client.post(
                    self._request_url(),
                    headers=self._request_headers(),
                    json=body,
                )
                response.raise_for_status()
        except (httpx.HTTPError, TimeoutError) as exc:
            raise self._failure(_classify_http_error(exc), exc, started) from exc
        except Exception as exc:
            classification = FailureClassification("PROVIDER_INTERNAL_ERROR", "REQUEST_SEND")
            raise self._failure(classification, exc, started) from exc
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            classification = FailureClassification("INVALID_RESPONSE", "RESPONSE_PARSE", response.status_code)
            raise self._failure(classification, exc, started) from exc
        try:
            content = extract_message_content(payload)
            usage = {key: int(value) for key, value in (payload.get("usage") or {}).items() if isinstance(value, (int, float))}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            classification = FailureClassification("INVALID_RESPONSE", "RESPONSE_PARSE", response.status_code)
            raise self._failure(classification, exc, started) from exc
        return ProviderCallResult(
            content, self.provider_name, self.model_name, usage,
            round((time.perf_counter() - started) * 1000), response.status_code,
        )

    async def semantic_interpret(self, query: str | ModelInput, context_slots: dict[str, Any]) -> ProviderCallResult:
        model_input = query if isinstance(query, ModelInput) else ModelInput(text=query)
        if model_input.has_multimodal_input and not self.capabilities.supports_multimodal:
            raise ProviderFailure("MODEL_UNAVAILABLE", self.provider_name)
        user_content: Any = model_input.text
        if model_input.has_multimodal_input:
            user_content = [{"type": "text", "text": model_input.text}]
            user_content.extend({"type": "image_url", "image_url": {"url": value}} for value in model_input.images)
            user_content.extend({"type": "video_url", "video_url": {"url": value}} for value in model_input.videos)
        return await self._chat(
            messages=[
                {"role": "system", "content": (
                    self.semantic_prompt
                    + "\n"
                    + semantic_output_contract()
                    + "\nsemantic_frame_json_schema="
                    + json.dumps(QueryUnderstanding.model_json_schema(), ensure_ascii=False)
                )},
                {"role": "user", "content": user_content if model_input.has_multimodal_input else json.dumps({"query": model_input.text, "context_slots": context_slots}, ensure_ascii=False)},
            ],
            max_tokens=self.semantic_max_tokens,
            request_options=self._semantic_request_options(),
        )

    async def reason(self, decision_packet: dict[str, Any]) -> ProviderCallResult:
        return await self._chat(
            messages=[
                {"role": "system", "content": self.reasoning_prompt + json.dumps(GroundedReasoningOutput.model_json_schema(), ensure_ascii=False)},
                {"role": "user", "content": json.dumps(decision_packet, ensure_ascii=False)},
            ],
            max_tokens=1200,
        )
