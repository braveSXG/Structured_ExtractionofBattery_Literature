"""Shared request settings for DeepSeek structured-output calls."""

from typing import Any, Dict, List


DEFAULT_MAX_OUTPUT_TOKENS = 32000


def create_structured_completion(
    client: Any,
    *,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    timeout: int,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> Any:
    """Request one JSON response without spending the budget on reasoning."""
    json_messages = [
        {"role": "system", "content": "Return exactly one valid json object."},
        *messages,
    ]
    return client.chat.completions.create(
        model=model,
        messages=json_messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
    )
