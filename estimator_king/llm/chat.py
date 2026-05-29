"""Chat provider that returns structured price estimates."""

import json

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from estimator_king.llm.config import ProviderConfig


class PriceRange(BaseModel):
    min: int
    max: int


class ReferenceProduct(BaseModel):
    name: str
    price_jpy: int
    store: str


class ProductEstimate(BaseModel):
    product_name: str
    suggested_price_jpy: int
    price_range_jpy: PriceRange
    confidence: str
    rationale: str
    reference_products: list[ReferenceProduct]


class EstimateBatch(BaseModel):
    estimates: list[ProductEstimate]


class EstimationError(Exception):
    """Raised when the chat model refuses or returns unparseable output."""


class ChatProvider:
    """Calls the chat model and returns a validated EstimateBatch.

    When chat_structured_output is True, uses chat.completions.parse with the
    EstimateBatch schema. Otherwise uses json_object mode and validates manually
    (for endpoints without strict schema support, e.g. ollama).
    """

    _config: ProviderConfig
    _client: OpenAI

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client = OpenAI(api_key=config.chat_api_key, base_url=config.chat_base_url)

    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if self._config.chat_structured_output:
            return self._estimate_structured(messages)
        return self._estimate_json_object(messages)

    def _estimate_structured(self, messages: list[dict[str, str]]) -> EstimateBatch:
        response = self._client.chat.completions.parse(
            model=self._config.chat_model,
            messages=messages,  # pyright: ignore[reportArgumentType]
            response_format=EstimateBatch,
        )
        message = response.choices[0].message
        if message.refusal:
            raise EstimationError(f"model refused: {message.refusal}")
        if message.parsed is None:
            raise EstimationError("structured output returned no parsed value")
        return message.parsed

    def _estimate_json_object(self, messages: list[dict[str, str]]) -> EstimateBatch:
        response = self._client.chat.completions.create(
            model=self._config.chat_model,
            messages=messages,  # pyright: ignore[reportArgumentType]
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        try:
            data: object = json.loads(content)
            return EstimateBatch.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise EstimationError(f"could not parse estimates: {exc}") from exc
