"""Item-type classifier provider (small/cheap model). Lazy OpenAI client.

The client is built on first ``classify_via_llm`` call, not at construction, so
the crawl path (which may have no chat/typing key) never raises at startup.
Two-tier orchestration and caching live in ``estimator_king.sync.typing``; this
class is only the LLM wrapper.
"""

import json
import logging

from openai import OpenAI

from estimator_king.llm.config import ProviderConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Role: You classify one Japanese merchandise item into exactly one category.\n\n"
    "# Goal\nPick the single best category for the given item text.\n\n"
    "<constraints>\n"
    "- Choose EXACTLY ONE value from this allowed list: {item_types}.\n"
    "- If none clearly fits, output \"その他\". Never invent a category outside the list.\n"
    "- Decide from the item name/description tokens; ignore talent names and event titles.\n"
    "</constraints>\n\n"
    "# Output\nReturn JSON only: {{\"item_type\": \"<one allowed value or その他>\"}}. No prose."
)


class TypingProvider:
    _config: ProviderConfig
    _client: OpenAI | None

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client = None  # lazy

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                api_key=self._config.typing_api_key,
                base_url=self._config.typing_base_url,
            )
        return self._client

    def classify_via_llm(self, text: str, item_types: list[str]) -> str:
        system = _SYSTEM_PROMPT.format(item_types=", ".join(item_types))
        response = self._get_client().chat.completions.create(
            model=self._config.typing_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        data = json.loads(content)
        return str(data.get("item_type", "その他"))
