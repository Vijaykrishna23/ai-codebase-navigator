from anthropic import AsyncAnthropic

from app.core.config import get_settings

_client: AsyncAnthropic | None = None


class LLMUnavailable(Exception):
    pass


def get_llm() -> AsyncAnthropic:
    """The Claude client, or LLMUnavailable when no API key is set."""
    global _client
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise LLMUnavailable(
            "Answering needs ANTHROPIC_API_KEY. Add it to .env and restart the backend."
        )
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client
