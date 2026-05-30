import logging
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
            "reference_products": [{"name": "ref", "price_jpy": 2000, "store": "hololive"}],
        }
    ]
}


@patch("estimator_king.llm.chat.OpenAI")
def test_debug_logged_on_success(mock_openai, caplog):
    client = mock_openai.return_value
    parsed = EstimateBatch.model_validate(VALID)
    msg = MagicMock(parsed=parsed, refusal=None)
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.chat"):
        ChatProvider(cfg).estimate("sys", "user")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.chat" and r.levelno == logging.DEBUG
    ]
    assert any(
        "chat request" in r.getMessage()
        and "structured=True" in r.getMessage()
        and "ms" in r.getMessage()
        for r in recs
    )


@patch("estimator_king.llm.chat.OpenAI")
def test_debug_logged_even_on_error(mock_openai, caplog):
    client = mock_openai.return_value
    msg = MagicMock(parsed=None, refusal="no")
    client.chat.completions.parse.return_value = MagicMock(choices=[MagicMock(message=msg)])
    cfg = ProviderConfig(embedding_api_key="k", chat_api_key="k", chat_structured_output=True)

    with caplog.at_level(logging.DEBUG, logger="estimator_king.llm.chat"):
        with pytest.raises(EstimationError):
            ChatProvider(cfg).estimate("sys", "user")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.llm.chat" and r.levelno == logging.DEBUG
    ]
    assert any(
        "chat request" in r.getMessage()
        and "structured=True" in r.getMessage()
        and "ms" in r.getMessage()
        for r in recs
    )
