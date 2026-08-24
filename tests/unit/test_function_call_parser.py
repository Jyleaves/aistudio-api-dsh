from aistudio_api.domain.models import parse_response_chunk


def test_parse_response_chunk_keeps_raw_function_call_and_response():
    chunk = [
        [
            [
                [
                    [
                        [None, None, None, ["getWeather", '{"city":"Shanghai"}']],
                        [None, None, None, None, ["getWeather", {"city": "Shanghai", "temperature": "24C"}]],
                    ]
                ],
                1,
            ]
        ],
        None,
        [5, 1, 6],
        None,
        None,
        None,
        None,
        "resp_123",
    ]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls == [
        {
            "type": "functionCall",
            "raw": ["getWeather", '{"city":"Shanghai"}'],
            "name": "getWeather",
            "args": {"city": "Shanghai"},
        }
    ]
    assert candidate.function_responses == [
        {
            "type": "functionResponse",
            "raw": ["getWeather", {"city": "Shanghai", "temperature": "24C"}],
            "name": "getWeather",
            "args": {"city": "Shanghai", "temperature": "24C"},
        }
    ]


def test_parse_response_chunk_extracts_real_aistudio_function_call_shape():
    chunk = [
        [
            [
                [
                    [
                        [
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            [
                                "getWeather",
                                [[["city", [None, None, "Shanghai"]]]],
                                "e6ni61kr",
                            ],
                            None,
                            None,
                            None,
                            "EiYKJGUyNDgzMGE3LTVjZDYtNDJmZS05OThiLWVlNTM5ZTcyYjljMw==",
                        ]
                    ],
                    "model",
                ]
            ]
        ],
        None,
        [52, 15, 147, None, [[1, 52]], None, None, None, None, 80],
        None,
        None,
        None,
        None,
        "resp_real",
    ]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls == [
        {
            "type": "functionCall",
            "raw": ["getWeather", [[["city", [None, None, "Shanghai"]]]], "e6ni61kr"],
            "name": "getWeather",
            "args": {"city": "Shanghai"},
            "call_id": "e6ni61kr",
            "thought_signature": "EiYKJGUyNDgzMGE3LTVjZDYtNDJmZS05OThiLWVlNTM5ZTcyYjljMw==",
        }
    ]


def test_parse_response_chunk_decodes_aistudio_array_argument_variant():
    raw_part = [None] * 10 + [[
        "web_search",
        [[[
            "queries", [None, None, None, None, None, [["alpha", "beta"]]]
        ]]],
        "call_1",
    ]]
    chunk = [
        [[[[raw_part], "model"]]],
        None, None, None, None, None, None, None,
    ]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls[0]["args"] == {"queries": ["alpha", "beta"]}


def test_parse_response_chunk_decodes_array_of_object_arguments():
    def scalar(value):
        return [None, None, value]

    def struct(**values):
        return [
            None,
            None,
            None,
            None,
            [[[key, scalar(value)] for key, value in values.items()]],
        ]

    todos = [
        struct(content="Inspect image input", status="in_progress"),
        struct(content="Repair credential loading", status="pending"),
        struct(content="Run regression tests", status="pending"),
        struct(content="Verify dsh end to end", status="pending"),
    ]
    raw_part = [None] * 10 + [[
        "todo_write",
        [[["todos", [None, None, None, None, None, todos]]]],
        "call_todos",
    ]]
    chunk = [
        [[[[raw_part], "model"]]],
        None, None, None, None, None, None, None,
    ]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls[0]["args"] == {
        "todos": [
            {"content": "Inspect image input", "status": "in_progress"},
            {"content": "Repair credential loading", "status": "pending"},
            {"content": "Run regression tests", "status": "pending"},
            {"content": "Verify dsh end to end", "status": "pending"},
        ]
    }


def test_parse_response_chunk_recursively_decodes_nested_objects_for_any_tool():
    def scalar(value):
        return [None, None, value]

    def array(*values):
        return [None, None, None, None, None, list(values)]

    def struct(values):
        return [
            None,
            None,
            None,
            None,
            [[[key, value] for key, value in values.items()]],
        ]

    operations = array(
        struct({
            "file_path": scalar("src/a.py"),
            "changes": array(
                struct({"old": scalar("alpha"), "new": scalar("beta")}),
                struct({"old": scalar("one"), "new": scalar("two")}),
            ),
        }),
        struct({
            "file_path": scalar("src/b.py"),
            "changes": array(struct({"old": scalar("x"), "new": scalar("y")})),
        }),
    )
    raw_part = [None] * 10 + [[
        "batch_edit",
        [[["operations", operations]]],
        "call_batch",
    ]]
    chunk = [[[[[raw_part], "model"]]], None, None, None, None, None, None, None]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls[0]["args"] == {
        "operations": [
            {
                "file_path": "src/a.py",
                "changes": [
                    {"old": "alpha", "new": "beta"},
                    {"old": "one", "new": "two"},
                ],
            },
            {
                "file_path": "src/b.py",
                "changes": [{"old": "x", "new": "y"}],
            },
        ]
    }
