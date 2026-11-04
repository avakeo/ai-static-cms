from google import genai
from typing import Protocol, Optional
import time
import logging

logger = logging.getLogger(__name__)

# --- 1. 共通インターフェース (Protocol) ---

def _extract_usage(response) -> Optional[dict]:
    """レスポンスから消費トークン数を取り出す。

    SDK のバージョンやモデルによって `usage_metadata` が無い / 一部の
    フィールドが None のことがある。ここで失敗しても生成そのものは成功して
    いるので、例外を投げずに None を返す（記録が欠けるだけにする）。
    """
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return None
    def _int(name):
        v = getattr(meta, name, None)
        return int(v) if isinstance(v, (int, float)) else 0
    prompt = _int("prompt_token_count")
    # 思考モデルは thoughts_token_count が別枠で課金対象になる。
    output = _int("candidates_token_count") + _int("thoughts_token_count")
    total = _int("total_token_count") or (prompt + output)
    if not total:
        return None
    return {"prompt": prompt, "output": output, "total": total}


class ArticleGenerator(Protocol):
    """
    記事（スレッド）を生成するための共通インターフェース
    generate メソッド（プロンプトを引数に文字列を返す）を持つ
    """
    def generate(self, prompt: str) -> str: ...

# --- 2. Gemini アカウント設定 ---

class GeminiAccountConfig:
    """一つの Gemini アカウント/プロジェクトの設定"""

    def __init__(self, api_key: str, project: Optional[str] = None, label: str = "primary"):
        if not api_key:
            raise ValueError(f"Gemini ({label}): APIキーが設定されていません。")
        self.api_key = api_key
        self.project = project
        self.label = label

# --- 3. 一時的エラーの判定 ---

_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

def _is_transient_error(exc: Exception) -> bool:
    """一時的なエラー（リトライ対象）かどうかを判定する"""
    exc_str = str(exc).lower()
    # Rate limit / quota
    if "429" in exc_str or "quota" in exc_str or "rate limit" in exc_str or "ratelimit" in exc_str:
        return True
    # Timeout
    if "timeout" in exc_str or "timed out" in exc_str or "deadline" in exc_str:
        return True
    # 5xx server errors
    for code in _TRANSIENT_STATUS_CODES:
        if str(code) in exc_str:
            return True
    # Network errors
    if any(k in exc_str for k in ("connection", "network", "socket", "ssl", "unavailable")):
        return True
    return False


def _is_project_denied(exc: Exception) -> bool:
    """このキー/アカウント自体が使えない状態（セカンダリへのフェイルオーバー対象）。

    2026-07-31、運用中のモードで「The bound service account is deleted or
    disabled」という401エラーがフェイルオーバーせず即失敗していた事故を
    受けて対象を広げた。元々は「project has been denied」という1文言しか
    見ていなかったが、これはリトライしても直らない「キー/アカウント自体の
    問題」全般（無効化・削除・認証エラー）を指すはずで、狭すぎた。
    """
    s = str(exc).lower()
    return any(k in s for k in (
        "your project has been denied", "project has been denied",
        "api_key_invalid", "invalid api key", "api key not valid",
        "unauthenticated", "permission denied",
        "service account is deleted", "service account is disabled",
        "401", "403",
    ))

# --- 4. Gemini の実装 ---

class GeminiGenerator:
    """
    Gemini AI の実装クラス。

    プライマリアカウントでリトライを試み、失敗かつセカンダリが設定されていれば
    セカンダリアカウントにフェイルオーバーする。
    """

    def __init__(
        self,
        primary: GeminiAccountConfig,
        model_name: str,
        secondary: Optional[GeminiAccountConfig] = None,
        failover_enabled: bool = True,
        retry_max_attempts: int = 3,
        retry_initial_delay: float = 2.0,
        retry_backoff_multiplier: float = 2.0,
    ):
        self.primary = primary
        self.model_name = model_name
        self.secondary = secondary
        self.failover_enabled = failover_enabled
        self.retry_max_attempts = retry_max_attempts
        self.retry_initial_delay = retry_initial_delay
        self.retry_backoff_multiplier = retry_backoff_multiplier
        # 直近の generate() でどちらのアカウントが実際に使われたか
        # （post_logに記録して、生成のたびにprimary/secondaryどちらを
        # 使ったか確認できるようにするため。2026-08-07）。
        self.last_used_account: Optional[str] = None
        # 直近の generate() が消費したトークン数。
        # {"prompt": int, "output": int, "total": int}。取れなければ None。
        #
        # generate() の戻り値（str）は Protocol で決まっているので変えられない。
        # last_used_account と同じく属性で持ち出す（2026-08-18、モード別・
        # 日別のコストを管理UIで見たいという要望）。
        self.last_usage: Optional[dict] = None

    def _build_client(self, account: GeminiAccountConfig) -> genai.Client:
        return genai.Client(api_key=account.api_key)

    def _generate_with_account(self, account: GeminiAccountConfig, prompt: str) -> str:
        """指定アカウントでリトライ付き生成を実行する"""
        delay = self.retry_initial_delay
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                logger.info(
                    "Gemini生成開始: アカウント=%s, 試行=%d/%d",
                    account.label, attempt, self.retry_max_attempts,
                )
                client = self._build_client(account)
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                )
                if not response.text:
                    # `response.text` はセーフティブロック・空candidate等で
                    # 例外を投げずに None を返すことがある（2026-09-03、運用中に
                    # 実際に発生: None が 下書き表の raw_text（NOT NULL）まで
                    # 素通りし、"NOT NULL constraint failed" という無関係に見える
                    # SQLエラーでクラッシュしていた）。ここで検知して、リトライ対象
                    # （_is_transient_error 非該当なので即失敗）として例外にする。
                    reason = getattr(
                        getattr(response, "candidates", [None])[0], "finish_reason", "不明"
                    ) if getattr(response, "candidates", None) else "候補なし"
                    raise ValueError(f"Geminiが空の応答を返しました（finish_reason={reason}）")
                self.last_used_account = account.label
                self.last_usage = _extract_usage(response)
                logger.info(
                    "Gemini生成成功: アカウント=%s, 試行=%d, トークン=%s",
                    account.label, attempt,
                    self.last_usage["total"] if self.last_usage else "不明",
                )
                return response.text
            except Exception as exc:
                last_exc = exc
                if not _is_transient_error(exc):
                    logger.error(
                        "Gemini生成失敗（リトライ不可）: アカウント=%s, エラー=%s",
                        account.label, exc,
                    )
                    raise
                if attempt < self.retry_max_attempts:
                    logger.warning(
                        "Gemini一時エラー: アカウント=%s, 試行=%d/%d, %d秒後にリトライ: %s",
                        account.label, attempt, self.retry_max_attempts, delay, exc,
                    )
                    time.sleep(delay)
                    delay *= self.retry_backoff_multiplier
                else:
                    logger.error(
                        "Geminiリトライ上限: アカウント=%s, 試行=%d/%d, エラー=%s",
                        account.label, attempt, self.retry_max_attempts, exc,
                    )

        assert last_exc is not None
        raise last_exc

    def generate(self, prompt: str) -> str:
        """プロンプトからテキストを生成する"""
        try:
            return self._generate_with_account(self.primary, prompt)
        except Exception as primary_exc:
            if (
                self.secondary is not None
                and self.failover_enabled
                and (_is_transient_error(primary_exc) or _is_project_denied(primary_exc))
            ):
                logger.warning(
                    "プライマリ失敗、セカンダリにフェイルオーバー: %s", primary_exc
                )
                return self._generate_with_account(self.secondary, prompt)
            raise
