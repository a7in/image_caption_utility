import auto_caption as ac


def test_settings_ini_roundtrip_preserves_types(tmp_path):
    """Saving then loading the .ini restores values with correct types."""
    path = str(tmp_path / "settings.ini")
    original = ac.LLMSettings(
        base_url="http://127.0.0.1:8080/v1",
        api_key="secret",
        model="Qwen3-VL",
        max_tokens=512,
        temperature=0.7,
        timeout=90.0,
        vision_image_format="jpeg",
    )
    original.save(path)

    loaded = ac.LLMSettings.load(path)

    assert loaded == original
    assert isinstance(loaded.max_tokens, int)
    assert isinstance(loaded.temperature, float)
    assert isinstance(loaded.timeout, float)


def test_settings_load_missing_file_returns_defaults(tmp_path):
    loaded = ac.LLMSettings.load(str(tmp_path / "does_not_exist.ini"))
    assert loaded == ac.LLMSettings()


def test_chat_completions_url_normalization():
    assert (ac._chat_completions_url("http://h:8080/v1")
            == "http://h:8080/v1/chat/completions")
    assert (ac._chat_completions_url("http://h:8080")
            == "http://h:8080/v1/chat/completions")
    # Already-complete URLs are left untouched (modulo trailing slash).
    full = "http://h:8080/v1/chat/completions"
    assert ac._chat_completions_url(full + "/") == full


def test_models_url_normalization():
    assert ac._models_url("http://h:8080/v1") == "http://h:8080/v1/models"
    assert ac._models_url("http://h:8080") == "http://h:8080/v1/models"
    assert ac._models_url("http://h:8080/v1/models/") == "http://h:8080/v1/models"
