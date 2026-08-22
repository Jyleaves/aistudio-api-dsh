from aistudio_api.api.state import RuntimeState


def test_runtime_stats_survive_repeated_reads_while_service_is_running():
    state = RuntimeState()
    state.record("gemini-3.7-flash", "success", {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    })
    first = dict(state.model_stats["gemini-3.7-flash"])
    second = dict(state.model_stats["gemini-3.7-flash"])
    assert first == second
    assert second["requests"] == 1
    assert second["success"] == 1


def test_runtime_stats_are_not_created_by_page_reads():
    state = RuntimeState()
    assert dict(state.model_stats) == {}
    _ = dict(state.model_stats)
    assert dict(state.model_stats) == {}
