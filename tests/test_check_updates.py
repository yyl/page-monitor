import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError
import requests

from scripts.check_updates import (
    ChapterUpdate,
    NotificationError,
    build_notification_lines,
    check_target,
    comix_update,
    main,
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
            update = check_target(target, timeout=20).latest

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

    def test_comix_jump_preserves_latest_link_and_points_to_title(self) -> None:
        latest = parse_comix_page(self.page_html(9, 125, "1h ago"), self.url)
        update = comix_update(latest, {"latest_issue": "Chapter 6.5"})

        lines = build_notification_lines([update])
        self.assertIn(latest.latest_issue_url, lines[1])
        self.assertIn("Previously Chapter 6.5", lines[2])
        self.assertTrue(lines[2].endswith(self.url))
        self.assertEqual(comix_update(latest, {"latest_issue": "Chapter 8"}), latest)


class TrackerRunTests(unittest.TestCase):
    manhuagui_url = "https://www.manhuagui.com/comic/34536/"
    comix_url = "https://comix.to/title/test-example-comic"
    old_chapter_url = manhuagui_url + "100.html"
    page = """
        <h1>Test Title</h1>
        <li class="status"><span>最近于 [<span class="red">2026-11-16</span>]
            更新至 [ <a href="/comic/34536/109.html" class="blue">第88话</a> ]</span></li>
        <a href="/comic/34536/109.html" title="第88话" class="status0"><span>第88话<i>20p</i></span></a>
        <a href="/comic/34536/100.html" title="第86话" class="status0">第86话</a>
        <a href="/comic/34536/104.html" title="第87话" class="status0">第87话</a>
        <a href="/comic/34536/104.html" title="第87话" class="status0">第87话</a>
        <a href="/comic/99999/110.html" title="Unrelated" class="status0">Unrelated</a>
    """

    def setUp(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        config = directory / "config.json"
        self.state_path = directory / "state.json"
        config.write_text(json.dumps({"targets": [
            {"type": "manhuagui", "url": self.manhuagui_url},
            {"type": "comix", "url": self.comix_url},
        ]}))
        self.enterContext(patch("sys.argv", [
            "check_updates.py", "--config-path", str(config), "--state-path", str(self.state_path),
        ]))
        self.enterContext(patch("builtins.print"))
        self.enterContext(patch("scripts.check_updates.time.sleep"))
        self.enterContext(patch("scripts.check_updates.fetch_html", side_effect=self.fetch_page))
        self.enterContext(patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.invalid"}))
        self.send = self.enterContext(patch("scripts.check_updates.send_discord_notification"))

    def fetch_page(self, url: str, timeout: int) -> str:
        if url == self.comix_url:
            raise URLError("Unavailable")
        return self.page

    def test_migrates_old_state_and_sends_all_new_links_once(self) -> None:
        comix_previous = {"latest_issue": "Chapter 6"}
        self.state_path.write_text(json.dumps({
            self.manhuagui_url: {"latest_issue_url": self.old_chapter_url},
            self.comix_url: comix_previous,
        }))

        self.assertEqual(main(), 0)
        self.send.assert_called_once()
        updates = self.send.call_args.args[1]
        self.assertEqual([update.latest_issue for update in updates], ["第87话", "第88话"])
        self.assertEqual([update.latest_issue_url for update in updates], [
            self.manhuagui_url + "104.html", self.manhuagui_url + "109.html",
        ])
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state[self.comix_url], comix_previous)
        self.assertEqual(state[self.manhuagui_url]["seen_chapter_ids"], [100, 104, 109])
        self.assertEqual(main(), 0)
        self.send.assert_called_once()

    def test_first_check_seeds_the_full_list_silently(self) -> None:
        self.assertEqual(main(), 0)
        self.send.assert_not_called()
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state[self.manhuagui_url]["seen_chapter_ids"], [100, 104, 109])

    def test_delivery_failure_keeps_the_batch_available_for_retry(self) -> None:
        previous = json.dumps({self.manhuagui_url: {"latest_issue_url": self.old_chapter_url}})
        self.state_path.write_text(previous)
        self.send.side_effect = NotificationError("Unavailable")

        self.assertEqual(main(), 1)
        self.assertEqual(self.state_path.read_text(), previous)
        self.send.side_effect = None
        self.assertEqual(main(), 0)
        self.assertEqual(self.send.call_args_list[0], self.send.call_args_list[1])


class DiscordNotificationTests(unittest.TestCase):
    def test_large_batch_attaches_every_chapter_in_one_message(self) -> None:
        updates = [
            ChapterUpdate("Test Title", "", "", f"Chapter {i}", f"https://example.org/chapter/{i}")
            for i in range(50)
        ]
        with patch("scripts.check_updates.requests.post") as post:
            send_discord_notification("https://discord.invalid", updates)

        post.assert_called_once()
        request = post.call_args.kwargs
        self.assertEqual(request["params"], {"wait": "true"})
        _, attachment, _ = request["files"]["files[0]"]
        self.assertEqual(attachment.decode("utf-8").splitlines(), build_notification_lines(updates))

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
