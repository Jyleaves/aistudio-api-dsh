from aistudio_api.infrastructure.gateway.model_catalog import filter_gemini_models, model_metadata


def test_model_catalog_keeps_only_gemini_and_normalizes_labels():
    assert filter_gemini_models([
        "Gemini 3.7 Flash",
        "gemini-3.1-pro-preview",
        "Gemma 4 31B",
        "Imagen 4",
        "Gemini_3.7_Flash",
    ]) == ["gemini-3.1-pro-preview", "gemini-3.7-flash"]


def test_model_metadata_exposes_dsh_capabilities():
    metadata = model_metadata("gemini-3.7-flash")
    assert metadata["contextWindow"] == 1_000_000
    assert metadata["maxTokens"] == 65_536
    assert metadata["name"] == "Gemini 3.7 Flash"
    assert metadata["context_window"] == 1_000_000
    assert metadata["max_output_tokens"] == 65_536
    assert metadata["reasoning"] is True
    assert metadata["inputModalities"] == ["text", "image"]


def test_model_catalog_rejects_ai_studio_description_text():
    assert filter_gemini_models([
        "gemini-3.7-flashgemini-3.7-flashhour-latest-and-most-capable-flash-model",
        "gemini-apipay-per-request-experiment-with-gemini-api-models-and-features-directly-in-the-ui",
        "gemini-3.7-flash",
    ]) == ["gemini-3.7-flash"]
