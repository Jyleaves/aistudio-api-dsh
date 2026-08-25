import json
from pathlib import Path

from aistudio_api.domain.models import parse_chunk_usage, parse_response_chunk, parse_text_output
from aistudio_api.infrastructure.gateway.stream_parser import IncrementalJSONStreamParser, classify_chunk


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests"


def test_parse_text_output_from_stream_bundle():
    raw = (FIXTURES / "test_output.json").read_text(encoding="utf-8")
    output = parse_text_output(raw)

    assert output.text == "你好！有什么我可以帮你的吗？"
    assert output.thinking.startswith('The user said "你好"')
    assert "Option 3 (Friendly)" in output.thinking
    assert output.usage["prompt_tokens"] == 5
    assert output.usage["completion_tokens"] == 161
    assert output.usage["total_tokens"] == 166
    assert output.usage["completion_tokens_details"]["reasoning_tokens"] == 153
    assert output.response_id
    assert output.candidates[0].finish_reason == 1


def test_parse_response_chunk_and_classify_chunk():
    raw = json.loads((FIXTURES / "test_output.json").read_text(encoding="utf-8"))
    final_chunk = raw[0][-1]

    candidate = parse_response_chunk(final_chunk)
    usage = parse_chunk_usage(final_chunk)
    assert candidate.finish_reason == 1
    assert candidate.safety_ratings
    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 161
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 153

    ctype, text = classify_chunk(raw[0][1])
    assert ctype == "thinking"
    assert 'standard Chinese greeting meaning "Hello."' in text


def test_stream_parser_extracts_real_chunks():
    raw = (FIXTURES / "test_output.json").read_text(encoding="utf-8")
    parser = IncrementalJSONStreamParser()

    chunks = list(parser.feed(raw))
    assert len(chunks) == 10
    assert classify_chunk(chunks[2])[0] == "thinking"
    assert classify_chunk(chunks[7])[0] == "body"


def test_parse_text_output_handles_double_wrapped_chunk_bundle():
    raw = json.dumps(
        [
            [
                [
                    [[[[[None, "先想一会儿", None, None, None, None, None, None, None, None, None, None, 1]], "model"]]],
                    None,
                    [10, None, 10, None, [[1, 10]]],
                    None,
                    None,
                    None,
                    None,
                    "resp_test",
                ],
                [
                    [[[[[None, "答案", None, None, None, None, None, None, None, None, None, None, 0]], "model"]]],
                    None,
                    [10, 2, 17, None, [[1, 10]], None, None, None, None, 5],
                    None,
                    None,
                    None,
                    None,
                    "resp_test",
                ],
            ]
        ]
    )

    output = parse_text_output(raw)

    assert output.thinking == "先想一会儿"
    assert output.text == "答案"
    assert output.usage["prompt_tokens"] == 10
    assert output.usage["completion_tokens"] == 7
    assert output.usage["total_tokens"] == 17
    assert output.usage["completion_tokens_details"]["reasoning_tokens"] == 5
    assert output.response_id == "resp_test"


def test_parse_response_chunk_preserves_google_search_grounding_metadata():
    raw_candidate = [
        [[[None, "Python 3.14.7 is current."],], "model"],
        1,
        None,
        None,
        [],
        None,
        None,
        [
            ["<style>.search{}</style>"],
            [
                [["https://vertexaisearch.cloud.google.com/grounding-api-redirect/source-1", "python.org"]],
            ],
            [
                [[None, 0, 25, "Python 3.14.7 is current."], [0]],
            ],
            None,
            ["current stable Python release"],
        ],
    ]
    candidate = parse_response_chunk([[raw_candidate]])

    assert candidate.grounding_metadata == {
        "searchEntryPoint": {"renderedContent": "<style>.search{}</style>"},
        "groundingChunks": [
            {
                "web": {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/source-1",
                    "title": "python.org",
                }
            }
        ],
        "groundingSupports": [
            {
                "segment": {"startIndex": 0, "endIndex": 25, "text": "Python 3.14.7 is current."},
                "groundingChunkIndices": [0],
            }
        ],
        "webSearchQueries": ["current stable Python release"],
    }
    assert candidate.url_context_metadata == {}


def test_parse_response_chunk_exposes_direct_url_context_metadata():
    raw_candidate = [
        [[[None, "Verified from the page."],], "model"],
        1,
        None,
        None,
        [],
        None,
        None,
        [
            None,
            [[["https://www.python.org/downloads/release/python-3147/", "Python 3.14.7"]]],
            [[[None, None, 23, "Verified from the page."], [0]]],
        ],
    ]
    candidate = parse_response_chunk([[raw_candidate]])

    assert candidate.url_context_metadata == {
        "urlMetadata": [
            {
                "retrievedUrl": "https://www.python.org/downloads/release/python-3147/",
                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
            }
        ]
    }
    assert candidate.grounding_metadata["groundingChunks"][0]["web"]["uri"].endswith("python-3147/")
