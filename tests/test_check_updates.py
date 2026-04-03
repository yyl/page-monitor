import unittest

from scripts.check_updates import parse_manhuagui_page


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


if __name__ == "__main__":
    unittest.main()

