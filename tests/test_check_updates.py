import json
import unittest
from unittest.mock import patch
import requests

from scripts.check_updates import (
    NotificationError,
    build_notification_lines,
    check_target,
    parse_comix_page,
    parse_manhuagui_page,
    send_discord_notification,
)


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


class ParseComixPageTests(unittest.TestCase):
    url = "https://comix.to/title/test-example-comic"

    def page_html(self, chapter: float, chapter_id: int, age: str) -> str:
        data = {
            "manga": {"hid": "test"},
            "queries": {
                '["manga","detail","test"]': {
                    "title": "Example Comic",
                    "latestChapter": chapter,
                    "latestChapterUrl": f"/title/test-example-comic/{chapter_id}-chapter-{chapter}",
                    "chapterUpdatedAtFormatted": age,
                },
            },
        }
        return f'<script type="application/json" id="initial-data">{json.dumps(data)}</script>'

    def test_dispatches_comix_and_formats_chapter_notification(self) -> None:
        target = {"type": "comix", "url": self.url}
        with patch("scripts.check_updates.fetch_html", return_value=self.page_html(6.5, 123, "1h ago")):
            update = check_target(target, timeout=20)

        self.assertEqual(update.url, self.url)
        self.assertEqual(
            build_notification_lines([update])[-1],
            f"- Example Comic: Chapter 6.5 {self.url}/123-chapter-6.5",
        )

    def test_fingerprint_tracks_chapter_changes_and_ignores_relative_age(self) -> None:
        earlier = parse_comix_page(self.page_html(6, 123, "1h ago"), self.url)
        later = parse_comix_page(self.page_html(6, 123, "2h ago"), self.url)
        new_chapter = parse_comix_page(self.page_html(7, 124, "1h ago"), self.url)

        self.assertEqual(earlier.fingerprint, later.fingerprint)
        self.assertNotEqual(later.fingerprint, new_chapter.fingerprint)

    def test_rejects_missing_chapter_metadata(self) -> None:
        html = '<script type="application/json" id="initial-data">{}</script>'
        with self.assertRaisesRegex(ValueError, "Invalid Comix title metadata"):
            parse_comix_page(html, self.url)


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
