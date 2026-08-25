import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aistudio_api.api.schemas import ChatRequest
from aistudio_api.application.api_service import handle_chat
from aistudio_api.domain.errors import RequestError


class _CaptureChatClient:
    def __init__(self):
        self.calls = []

    async def generate_content(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        class _Output:
            text = "ok"
            thinking = ""
            usage = {}
            function_calls = []

        return _Output()


@asynccontextmanager
async def _lease_test_client(client, **_kwargs):
    yield SimpleNamespace(client=client, account_id=None, worker_id="test")


def test_handle_chat_empty_tools_disables_model_defaults(monkeypatch):
    from aistudio_api.application import api_service_openai

    monkeypatch.setattr(api_service_openai, "acquire_request_client", _lease_test_client)
    monkeypatch.setattr(api_service_openai, "record_rotator_event", lambda *args, **kwargs: None)

    client = _CaptureChatClient()
    req = ChatRequest(
        model="gemma-4-31b-it",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
    )

    asyncio.run(handle_chat(req, client))

    assert len(client.calls) == 1
    assert client.calls[0]["kwargs"]["tools"] == []


def test_handle_chat_retries_small_budget_empty_reasoning_once(monkeypatch):
    from aistudio_api.application import api_service_openai

    monkeypatch.setattr(api_service_openai, "acquire_request_client", _lease_test_client)
    monkeypatch.setattr(api_service_openai, "record_rotator_event", lambda *args, **kwargs: None)

    class Client:
        def __init__(self):
            self.max_tokens = []

        async def generate_content(self, **kwargs):
            self.max_tokens.append(kwargs["max_tokens"])
            return SimpleNamespace(
                text="" if len(self.max_tokens) == 1 else "visible",
                thinking="hidden" if len(self.max_tokens) == 1 else "",
                usage={},
                function_calls=[],
            )

    client = Client()
    request = ChatRequest(
        model="gemini-3.7-flash",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=64,
    )

    response = asyncio.run(handle_chat(request, client))

    assert response.choices[0].message.content == "visible"
    assert client.max_tokens == [64, 512]


def test_stream_retries_ambiguous_capture_404_before_output(monkeypatch):
    from aistudio_api.application import api_service_openai

    monkeypatch.setattr(api_service_openai, "acquire_request_client", _lease_test_client)
    monkeypatch.setattr(api_service_openai, "record_rotator_event", lambda *args, **kwargs: None)

    class Client:
        def __init__(self):
            self.attempts = 0
            self.clears = 0

        def clear_snapshot_cache(self):
            self.clears += 1

        async def stream_generate_content(self, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RequestError(
                    404,
                    "Ambiguous request for service '' and method '/GenerativeService.StreamGenerateContent'",
                )
            yield "body", "recovered"

    async def scenario():
        client = Client()
        request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            tools=[],
        )
        response = await handle_chat(request, client)
        body = "".join([chunk async for chunk in response.body_iterator])
        assert "recovered" in body
        assert client.attempts == 2
        assert client.clears == 1

    asyncio.run(scenario())


def test_capture_retry_classification_is_narrow():
    from aistudio_api.application.api_service_common import MAX_RETRIES, capture_retry_reason

    ambiguous = RequestError(
        404,
        "Ambiguous request for service '' and method '/GenerativeService.StreamGenerateContent'",
    )
    assert capture_retry_reason(ambiguous, attempt=0, has_yielded_data=False) == "ambiguous_service"
    assert capture_retry_reason(ambiguous, attempt=0, has_yielded_data=True) is None
    assert capture_retry_reason(ambiguous, attempt=MAX_RETRIES - 1, has_yielded_data=False) is None
    assert capture_retry_reason(RequestError(404, "Not found"), attempt=0, has_yielded_data=False) is None


def test_gemini_stream_retries_ambiguous_capture_404_before_output(monkeypatch):
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application import api_service_gemini

    monkeypatch.setattr(api_service_gemini, "acquire_request_client", _lease_test_client)
    monkeypatch.setattr(api_service_gemini, "record_rotator_event", lambda *args, **kwargs: None)

    class Client:
        def __init__(self):
            self.attempts = 0
            self.clears = 0

        def clear_snapshot_cache(self):
            self.clears += 1

        async def stream_generate_content(self, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RequestError(
                    404,
                    "Ambiguous request for service '' and method '/GenerativeService.StreamGenerateContent'",
                )
            yield "body", "recovered-native"

    async def scenario():
        client = Client()
        request = GeminiGenerateContentRequest(
            contents=[{"role": "user", "parts": [{"text": "hello"}]}],
        )
        response = await api_service_gemini.handle_gemini_generate_content(
            "gemini-3.7-flash",
            request,
            client,
            stream=True,
        )
        body = "".join([chunk async for chunk in response.body_iterator])
        assert "recovered-native" in body
        assert client.attempts == 2
        assert client.clears == 1

    asyncio.run(scenario())


def test_normalize_chat_request_tool_calls_and_responses():
    from aistudio_api.application.chat_service import normalize_chat_request
    from aistudio_api.api.schemas import Message

    messages = [
        Message(role="user", content="What is the weather like in Beijing?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"location": "Beijing"}'},
                }
            ],
        ),
        Message(
            role="tool",
            tool_call_id="call_123",
            name="get_weather",
            content='{"temperature": 24, "condition": "sunny"}',
        ),
    ]

    res = normalize_chat_request(messages, "gemini-3.5-flash")
    contents = res["contents"]

    # 当前实现走文本协议：assistant 的 tool_calls 消息不产生独立 content，
    # tool 结果以 <tool_result> 标记的 user 消息传给模型。
    assert len(contents) == 2

    # Check User Message
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "What is the weather like in Beijing?"

    # Check Tool Response Message（带函数名标记）
    assert contents[1].role == "user"
    assert '<tool_result name="get_weather">' in contents[1].parts[0].text
    assert '"temperature": 24' in contents[1].parts[0].text

