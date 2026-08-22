from aistudio_api.infrastructure.gateway.model_catalog import filter_gemini_models


def test_model_catalog_keeps_only_gemini_and_normalizes_labels():
    assert filter_gemini_models([
        "Gemini 3.7 Flash",
        "gemini-3.1-pro-preview",
        "Gemma 4 31B",
        "Imagen 4",
        "Gemini_3.7_Flash",
    ]) == ["gemini-3.1-pro-preview", "gemini-3.7-flash"]
