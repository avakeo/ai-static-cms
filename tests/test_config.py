"""モード定義の検証。"""
import pytest

from cms import config
from cms.schema import ValidationError


def test_sample_modes_are_valid():
    modes = config.load_modes()
    assert set(modes) == {"tech-news", "history-trivia"}
    assert modes["tech-news"]["publish_order"] == "fifo"


def test_prompts_have_the_expected_placeholders():
    modes = config.load_modes()
    assert "{TITLE}" in config.load_prompt(modes["tech-news"])
    assert "{ITEM}" in config.load_prompt(modes["history-trivia"])


def base():
    return {"site_title": "s", "base_url": "u", "type": "rss", "prompt_file": "prompts/x.txt",
            "schedule": "0 7 * * *", "publish_schedule": "0 8 * * *", "rss_url": "https://e/x"}


def test_unknown_key_is_rejected_with_suggestion():
    with pytest.raises(ValidationError, match="もしかして: rss_url"):
        config.validate_mode({**base(), "rss_ulr": "x"})


def test_field_of_another_source_is_rejected():
    with pytest.raises(ValidationError, match="items_file"):
        config.validate_mode({**base(), "items_file": "items/x.txt"})


def test_all_errors_are_reported_together():
    data = base()
    del data["site_title"], data["rss_url"]
    with pytest.raises(ValidationError) as e:
        config.validate_mode(data)
    assert "サイト名" in str(e.value) and "RSS URL" in str(e.value)


def test_crontab_lists_generate_retry_and_publish():
    from cms.cli import crontab_lines
    lines = crontab_lines(config.load_modes())
    assert "*/5 * * * * cms publish-due" in lines
    assert "0 7 * * * cms generate tech-news" in lines
    assert "0 8,12,18 * * * cms publish tech-news" in lines
    assert any(line.endswith("cms generate tech-news --retry") for line in lines)
