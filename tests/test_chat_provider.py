import json
from unittest.mock import MagicMock, patch

import pytest

from estimator_king.llm.chat import ChatProvider, EstimateBatch, EstimationError
from estimator_king.llm.config import ProviderConfig

VALID = {
    "estimates": [
        {
            "product_name": "p1",
            "suggested_price_jpy": 2000,
            "price_range_jpy": {"min": 1800, "max": 2200},
            "confidence": "high",
            "rationale": "because",
            "reference_products": [
                {"name": "ref", "price_jpy": 2000, "store": "hololive"}
            ],
        }
    ]
}


@patch("estimator_king.llm.chat.OpenAI")
def test_structured_output_uses_parse_and_returns_batch(mock_openai):
    client = mock_openai.return_value
    parsed = EstimateBatch.model_validate(VALID)
    msg = MagicMock(parsed=parsed, refusal=None)
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    out = ChatProvider(cfg).estimate("sys", "user")

    assert isinstance(out, EstimateBatch)
    assert out.estimates[0].suggested_price_jpy == 2000
    kwargs = client.chat.completions.parse.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["response_format"] is EstimateBatch


@patch("estimator_king.llm.chat.OpenAI")
def test_structured_output_refusal_raises(mock_openai):
    client = mock_openai.return_value
    msg = MagicMock(parsed=None, refusal="no")
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    with pytest.raises(EstimationError):
        ChatProvider(cfg).estimate("sys", "user")


@patch("estimator_king.llm.chat.OpenAI")
def test_json_object_mode_parses_content(mock_openai):
    client = mock_openai.return_value
    msg = MagicMock(content=json.dumps(VALID))
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=False)

    out = ChatProvider(cfg).estimate("sys", "user")

    assert out.estimates[0].confidence == "high"
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


@patch("estimator_king.llm.chat.OpenAI")
def test_json_object_mode_invalid_json_raises(mock_openai):
    client = mock_openai.return_value
    msg = MagicMock(content="not json")
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=False)

    with pytest.raises(EstimationError):
        ChatProvider(cfg).estimate("sys", "user")
