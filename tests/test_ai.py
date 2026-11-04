"""Tests for retry and failover logic in src/cms/ai.py"""
import pytest
from unittest.mock import MagicMock, patch, call

from cms.ai import GeminiAccountConfig, GeminiGenerator, _is_transient_error, _is_project_denied


# --- _is_transient_error tests ---

class TestIsTransientError:
    def test_rate_limit_429(self):
        assert _is_transient_error(Exception("HTTP 429 Too Many Requests")) is True

    def test_quota_exceeded(self):
        assert _is_transient_error(Exception("quota exceeded")) is True

    def test_timeout(self):
        assert _is_transient_error(Exception("Request timed out")) is True

    def test_deadline(self):
        assert _is_transient_error(Exception("deadline exceeded")) is True

    def test_500_server_error(self):
        assert _is_transient_error(Exception("500 Internal Server Error")) is True

    def test_503_unavailable(self):
        assert _is_transient_error(Exception("503 Service Unavailable")) is True

    def test_connection_error(self):
        assert _is_transient_error(Exception("connection reset by peer")) is True

    def test_network_error(self):
        assert _is_transient_error(Exception("network error")) is True

    def test_invalid_credentials_not_transient(self):
        assert _is_transient_error(Exception("invalid API key")) is False

    def test_bad_request_not_transient(self):
        assert _is_transient_error(Exception("400 Bad Request")) is False

    def test_content_policy_not_transient(self):
        assert _is_transient_error(Exception("content policy violation")) is False


# --- _is_project_denied tests ---

class TestIsProjectDenied:
    def test_project_denied_phrase(self):
        assert _is_project_denied(Exception("Your project has been denied access")) is True

    def test_service_account_deleted(self):
        # 2026-07-31: 運用中に実際に発生したエラー文言
        assert _is_project_denied(Exception(
            "401 Unauthenticated: The bound service account is deleted or disabled."
        )) is True

    def test_service_account_disabled(self):
        assert _is_project_denied(Exception("The bound service account is disabled.")) is True

    def test_invalid_api_key(self):
        assert _is_project_denied(Exception("API_KEY_INVALID")) is True
        assert _is_project_denied(Exception("invalid API key")) is True

    def test_unauthenticated(self):
        assert _is_project_denied(Exception("401 Unauthenticated")) is True

    def test_permission_denied(self):
        assert _is_project_denied(Exception("403 Permission denied")) is True

    def test_unrelated_error_not_matched(self):
        assert _is_project_denied(Exception("content policy violation")) is False
        assert _is_project_denied(Exception("429 rate limit")) is False


# --- GeminiAccountConfig tests ---

class TestGeminiAccountConfig:
    def test_requires_api_key(self):
        with pytest.raises(ValueError):
            GeminiAccountConfig(api_key="", label="primary")

    def test_valid_config(self):
        cfg = GeminiAccountConfig(api_key="key123", project="proj1", label="primary")
        assert cfg.api_key == "key123"
        assert cfg.project == "proj1"
        assert cfg.label == "primary"


# --- GeminiGenerator tests ---

def _make_generator(
    primary_key="pk",
    secondary_key=None,
    failover_enabled=True,
    retry_max_attempts=3,
    retry_initial_delay=0.0,
    retry_backoff_multiplier=2.0,
):
    primary = GeminiAccountConfig(api_key=primary_key, label="primary")
    secondary = (
        GeminiAccountConfig(api_key=secondary_key, label="secondary")
        if secondary_key
        else None
    )
    return GeminiGenerator(
        primary=primary,
        model_name="gemini-test",
        secondary=secondary,
        failover_enabled=failover_enabled,
        retry_max_attempts=retry_max_attempts,
        retry_initial_delay=retry_initial_delay,
        retry_backoff_multiplier=retry_backoff_multiplier,
    )


class TestGeminiGeneratorRetry:
    def test_success_on_first_attempt(self):
        gen = _make_generator()
        mock_response = MagicMock()
        mock_response.text = "generated text"
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = mock_model
            result = gen.generate("test prompt")

        assert result == "generated text"
        assert mock_model.generate_content.call_count == 1
        mock_model.generate_content.assert_called_once_with(
            model="gemini-test", contents="test prompt"
        )

    def test_retries_on_transient_error_then_succeeds(self):
        gen = _make_generator(retry_max_attempts=3)
        mock_response = MagicMock()
        mock_response.text = "ok"
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = [
            Exception("429 rate limit"),
            mock_response,
        ]

        with patch("cms.ai.genai") as mock_genai, patch("cms.ai.time.sleep"):
            mock_genai.Client.return_value.models = mock_model
            result = gen.generate("prompt")

        assert result == "ok"
        assert mock_model.generate_content.call_count == 2

    def test_exhausts_retries_and_raises(self):
        gen = _make_generator(retry_max_attempts=3)
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("503 Service Unavailable")

        with patch("cms.ai.genai") as mock_genai, patch("cms.ai.time.sleep"):
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(Exception, match="503"):
                gen.generate("prompt")

        assert mock_model.generate_content.call_count == 3

    def test_no_retry_on_non_transient_error(self):
        gen = _make_generator(retry_max_attempts=3)
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("invalid API key")

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(Exception, match="invalid API key"):
                gen.generate("prompt")

        assert mock_model.generate_content.call_count == 1


class TestGeminiGeneratorFailover:
    def test_failover_to_secondary_on_transient_error(self):
        gen = _make_generator(
            primary_key="pk",
            secondary_key="sk",
            failover_enabled=True,
            retry_max_attempts=1,
        )
        mock_response = MagicMock()
        mock_response.text = "secondary result"

        primary_model = MagicMock()
        primary_model.generate_content.side_effect = Exception("429 rate limit")

        secondary_model = MagicMock()
        secondary_model.generate_content.return_value = mock_response

        call_count = {"n": 0}

        def fake_client(**_kwargs):
            call_count["n"] += 1
            client = MagicMock()
            client.models = primary_model if call_count["n"] == 1 else secondary_model
            return client

        with patch("cms.ai.genai") as mock_genai, patch("cms.ai.time.sleep"):
            mock_genai.Client.side_effect = fake_client
            result = gen.generate("prompt")

        assert result == "secondary result"

    def test_no_failover_when_disabled(self):
        gen = _make_generator(
            primary_key="pk",
            secondary_key="sk",
            failover_enabled=False,
            retry_max_attempts=1,
        )
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("429 rate limit")

        with patch("cms.ai.genai") as mock_genai, patch("cms.ai.time.sleep"):
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(Exception, match="429"):
                gen.generate("prompt")

        assert mock_model.generate_content.call_count == 1

    def test_no_failover_when_no_secondary(self):
        gen = _make_generator(
            primary_key="pk",
            secondary_key=None,
            failover_enabled=True,
            retry_max_attempts=1,
        )
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("503 error")

        with patch("cms.ai.genai") as mock_genai, patch("cms.ai.time.sleep"):
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(Exception, match="503"):
                gen.generate("prompt")

    def test_no_failover_on_unrelated_error(self):
        # content policy violation等、transientでもkey/account由来でもない
        # エラーはフェイルオーバーしない
        gen = _make_generator(
            primary_key="pk",
            secondary_key="sk",
            failover_enabled=True,
            retry_max_attempts=1,
        )
        primary_model = MagicMock()
        primary_model.generate_content.side_effect = Exception("content policy violation")

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = primary_model
            with pytest.raises(Exception, match="content policy violation"):
                gen.generate("prompt")

        assert primary_model.generate_content.call_count == 1

    def test_failover_on_invalid_api_key(self):
        # 2026-07-31: 運用中に「service account is deleted or
        # disabled」がフェイルオーバーせず即失敗していた事故の再発防止。
        # キー/アカウント自体が無効というエラーはリトライしても直らないが、
        # セカンダリアカウントには切り替える価値がある
        gen = _make_generator(
            primary_key="pk",
            secondary_key="sk",
            failover_enabled=True,
            retry_max_attempts=1,
        )
        mock_response = MagicMock()
        mock_response.text = "secondary result"

        primary_model = MagicMock()
        primary_model.generate_content.side_effect = Exception(
            "401 Unauthenticated: The bound service account is deleted or disabled."
        )
        secondary_model = MagicMock()
        secondary_model.generate_content.return_value = mock_response

        call_count = {"n": 0}

        def fake_client(**_kwargs):
            call_count["n"] += 1
            client = MagicMock()
            client.models = primary_model if call_count["n"] == 1 else secondary_model
            return client

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.side_effect = fake_client
            result = gen.generate("prompt")

        assert result == "secondary result"
        assert primary_model.generate_content.call_count == 1


class TestLastUsedAccount:
    """2026-08-07: post_logにprimary/secondaryどちらのアカウントで
    生成したか記録するため、GeminiGenerator側で直近の使用アカウントを
    追跡できるようにした。"""

    def test_none_before_any_generate_call(self):
        gen = _make_generator()
        assert gen.last_used_account is None

    def test_records_primary_on_success(self):
        gen = _make_generator()
        mock_response = MagicMock()
        mock_response.text = "ok"
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = mock_model
            gen.generate("prompt")

        assert gen.last_used_account == "primary"

    def test_records_secondary_after_failover(self):
        gen = _make_generator(
            primary_key="pk", secondary_key="sk",
            failover_enabled=True, retry_max_attempts=1,
        )
        mock_response = MagicMock()
        mock_response.text = "secondary result"

        primary_model = MagicMock()
        primary_model.generate_content.side_effect = Exception("429 rate limit")
        secondary_model = MagicMock()
        secondary_model.generate_content.return_value = mock_response

        call_count = {"n": 0}

        def fake_client(**_kwargs):
            call_count["n"] += 1
            client = MagicMock()
            client.models = primary_model if call_count["n"] == 1 else secondary_model
            return client

        with patch("cms.ai.genai") as mock_genai, patch("cms.ai.time.sleep"):
            mock_genai.Client.side_effect = fake_client
            gen.generate("prompt")

        assert gen.last_used_account == "secondary"

class TestEmptyResponseText:
    """2026-09-03: `response.text` はセーフティブロック・空candidate等で
    例外を投げずに None を返すことがある。それがそのまま呼び出し元へ
    戻ると draft_threads.raw_text（NOT NULL）への INSERT が
    "NOT NULL constraint failed" という無関係に見えるSQLエラーで
    クラッシュしていた（運用中に実際に発生、原因特定に時間がかかった）。
    generate() の時点で検知して、意味の分かる例外にする。"""

    def test_none_text_raises_instead_of_returning_none(self):
        gen = _make_generator()
        mock_response = MagicMock()
        mock_response.text = None
        mock_response.candidates = []
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(ValueError, match="空の応答"):
                gen.generate("prompt")

    def test_empty_string_text_also_raises(self):
        gen = _make_generator()
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.candidates = []
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(ValueError, match="空の応答"):
                gen.generate("prompt")

    def test_not_treated_as_transient_no_retry_no_failover(self):
        """空応答は再試行してもまず変わらないので、リトライもフェイルオーバーもしない。"""
        gen = _make_generator(
            primary_key="pk", secondary_key="sk",
            failover_enabled=True, retry_max_attempts=3,
        )
        mock_response = MagicMock()
        mock_response.text = None
        mock_response.candidates = []
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = mock_model
            with pytest.raises(ValueError):
                gen.generate("prompt")

        assert mock_model.generate_content.call_count == 1  # リトライしていない
        assert gen.last_used_account is None                # フェイルオーバーもしていない


class TestGemini3xModelIds:
    @pytest.mark.parametrize("model_name", ["gemini-3.6-flash", "gemini-3.7-flash"])
    def test_forwards_model_id_without_sampling_config(self, model_name):
        gen = _make_generator()
        gen.model_name = model_name
        response = MagicMock(text="ok")
        models = MagicMock()
        models.generate_content.return_value = response

        with patch("cms.ai.genai") as mock_genai:
            mock_genai.Client.return_value.models = models
            assert gen.generate("prompt") == "ok"

        models.generate_content.assert_called_once_with(
            model=model_name, contents="prompt"
        )
