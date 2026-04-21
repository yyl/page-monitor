import unittest
from unittest.mock import call
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import requests

from scripts.check_updates import (
    FETCH_RETRY_ATTEMPTS,
    FETCH_RETRY_DELAY_SECONDS,
    NotificationError,
    fetch_html,
    parse_manhuagui_page,
    send_discord_notification,
)


class UrllibResponseStub:
    def __init__(self, body: bytes, charset: str = "utf-8") -> None:
        self._body = body
        self.headers = self
        self._charset = charset

    def __enter__(self) -> "UrllibResponseStub":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def get_content_charset(self) -> str:
        return self._charset

    def read(self) -> bytes:
        return self._body


class ParseManhuaguiPageTests(unittest.TestCase):
    def test_extracts_status_update_fields(self) -> None:
        html = """
        <html>
          <body>
            <h1>间谍过家家</h1>
            <ul>
              <li class="status"><span><strong>漫画状态：</strong><span class="red">连载中</span>。最近于 [<span class="red">2026-03-31</span>] 更新至 [ <a href="/comic/31550/873604.html" target="_blank" class="blue">第131话</a> ]。间谍过家家132 待更新</span></li>
            </ul>
          </body>
        </html>
        """

        parsed = parse_manhuagui_page(html, "https://www.manhuagui.com/comic/31550/")

        self.assertEqual(parsed.name, "间谍过家家")
        self.assertEqual(parsed.updated_date, "2026-03-31")
        self.assertEqual(parsed.latest_issue, "第131话")
        self.assertEqual(
            parsed.latest_issue_url,
            "https://www.manhuagui.com/comic/31550/873604.html",
        )

    def test_prefers_configured_name_when_present(self) -> None:
        html = """
        <html>
          <body>
            <h1>Fallback Title</h1>
            <li class="status"><span>最近于 [<span class="red">2026-04-01</span>] 更新至 [ <a href="/comic/1/2.html" class="blue">第1话</a> ]</span></li>
          </body>
        </html>
        """

        parsed = parse_manhuagui_page(
            html,
            "https://www.manhuagui.com/comic/1/",
            configured_name="Configured Name",
        )

        self.assertEqual(parsed.name, "Configured Name")
        self.assertEqual(parsed.updated_date, "2026-04-01")
        self.assertEqual(parsed.latest_issue, "第1话")
        self.assertEqual(parsed.latest_issue_url, "https://www.manhuagui.com/comic/1/2.html")


class FetchHtmlTests(unittest.TestCase):
    def test_retries_transient_url_errors(self) -> None:
        response = UrllibResponseStub(b"<html>ok</html>")

        with (
            patch(
                "scripts.check_updates.urlopen",
                side_effect=[URLError(TimeoutError("timed out")), response],
            ) as mock_urlopen,
            patch("scripts.check_updates.time.sleep") as mock_sleep,
        ):
            html = fetch_html("https://example.com", timeout=20)

        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(FETCH_RETRY_DELAY_SECONDS)

    def test_raises_after_exhausting_retries(self) -> None:
        error = URLError(TimeoutError("timed out"))
        attempts = FETCH_RETRY_ATTEMPTS

        with (
            patch("scripts.check_updates.urlopen", side_effect=[error] * attempts),
            patch("scripts.check_updates.time.sleep") as mock_sleep,
        ):
            with self.assertRaises(URLError):
                fetch_html("https://example.com", timeout=20)

        self.assertEqual(mock_sleep.call_count, attempts - 1)
        self.assertEqual(
            mock_sleep.call_args_list,
            [call(FETCH_RETRY_DELAY_SECONDS)] * (attempts - 1),
        )

    def test_does_not_retry_http_errors(self) -> None:
        error = HTTPError(
            url="https://example.com",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

        with (
            patch("scripts.check_updates.urlopen", side_effect=error) as mock_urlopen,
            patch("scripts.check_updates.time.sleep") as mock_sleep,
        ):
            with self.assertRaises(HTTPError):
                fetch_html("https://example.com", timeout=20)

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()


class DiscordNotificationTests(unittest.TestCase):
    def test_surfaces_discord_error_details(self) -> None:
        update = parse_manhuagui_page(
            """
            <html>
              <body>
                <h1>Test Title</h1>
                <li class="status"><span>最近于 [<span class="red">2026-04-01</span>] 更新至 [ <a href="/comic/1/2.html" class="blue">第1话</a> ]</span></li>
              </body>
            </html>
            """,
            "https://www.manhuagui.com/comic/1/",
        )

        response = requests.Response()
        response.status_code = 403
        response.reason = "Forbidden"
        response.url = "https://discord.com/api/webhooks/test"
        response._content = b'{"message":"Unknown Webhook","code":10015}'
        error = requests.HTTPError("403 Client Error: Forbidden for url", response=response)

        with patch("scripts.check_updates.requests.post", side_effect=error):
            with self.assertRaises(NotificationError) as context:
                send_discord_notification("https://discord.com/api/webhooks/test", [update])

        message = str(context.exception)
        self.assertIn("HTTP 403 Forbidden", message)
        self.assertIn("Unknown Webhook", message)
        self.assertIn("invalid, revoked, or no longer allowed", message)


if __name__ == "__main__":
    unittest.main()
