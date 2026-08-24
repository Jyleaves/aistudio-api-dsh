import pytest
from pydantic import ValidationError

from aistudio_api.api.schemas import ChatRequest
from aistudio_api.application.chat_service import normalize_openai_reasoning_effort


@pytest.mark.parametrize(
    ("effort", "wire_level"),
    [("minimal", 4), ("low", 1), ("medium", 2), ("high", 3)],
)
def test_openai_reasoning_effort_maps_to_ai_studio_wire_level(effort, wire_level):
    request = ChatRequest(
        model="gemini-3.7-flash",
        messages=[{"role": "user", "content": "think"}],
        reasoning_effort=effort,
    )

    assert normalize_openai_reasoning_effort(request.reasoning_effort) == [
        1,
        None,
        None,
        wire_level,
    ]


def test_openai_reasoning_effort_rejects_unsupported_levels():
    with pytest.raises(ValidationError):
        ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "think"}],
            reasoning_effort="max",
        )
