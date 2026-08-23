from muxiva_codex_relay.normalizer import TranscriptNormalizer, should_use_s1_mini


def test_s1_is_only_selected_for_english() -> None:
    assert should_use_s1_mini("um please fix the login test")
    assert not should_use_s1_mini("帮我修复登录测试")
    assert not should_use_s1_mini("嗯")


def test_chinese_passes_through_without_model_call() -> None:
    result = TranscriptNormalizer("http://127.0.0.1:1/v1", "s1-mini", 1).normalize("帮我修复登录测试")
    assert result.text == "帮我修复登录测试"
    assert result.engine == "asr-original"


def test_empty_transcript_is_valid() -> None:
    result = TranscriptNormalizer(None, "s1-mini").normalize("   ")
    assert result.text == ""
    assert result.engine == "empty"
