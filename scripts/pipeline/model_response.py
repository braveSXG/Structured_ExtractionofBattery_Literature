"""Small helpers shared by the OpenAI-compatible model callers."""


def read_response_text(response):
    choice = response.choices[0]
    message = choice.message
    content = getattr(message, "content", None) or ""
    reasoning = getattr(message, "reasoning_content", None) or ""
    diagnostic = (
        f"finish_reason={getattr(choice, 'finish_reason', None)}, "
        f"reasoning_content_chars={len(reasoning)}"
    )
    return content, diagnostic
