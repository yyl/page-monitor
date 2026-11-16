#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from html import unescape
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
import requests
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


MANHUAGUI_BASE_URL = "https://www.manhuagui.com"
DEFAULT_CONFIG_PATH = Path("config/tracker.json")
DEFAULT_STATE_PATH = Path(".cache/tracker/state.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

STATUS_BLOCK_RE = re.compile(r'<li\s+class="status">\s*(.*?)\s*</li>', re.DOTALL)
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
DATE_RE = re.compile(
    r"""最近于\s*\[\s*<span[^>]*class=["']red["'][^>]*>\s*([^<]+?)\s*</span>\s*\]""",
    re.DOTALL,
)
ISSUE_RE = re.compile(
    r"""更新至\s*\[\s*<a\s+href=["']([^"']+)["'][^>]*class=["']blue["'][^>]*>\s*([^<]+?)\s*</a>\s*\]""",
    re.DOTALL,
)
MANHUAGUI_CHAPTER_RE = re.compile(
    r'<a\s+href="(/comic/\d+/(\d+)\.html)"\s+title="([^"]*)"\s+class="status\d+"'
)
COMIX_INITIAL_DATA_RE = re.compile(
    r"""<script\b[^>]*\s+id=["']initial-data["'][^>]*>(.*?)</script>""",
    re.DOTALL | re.IGNORECASE,
)
COMIX_CHAPTER_NUMBER_RE = re.compile(r"Chapter (\d+(?:\.\d+)?)")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
TrackerState = dict[str, dict[str, Any]]


@dataclass
class ChapterUpdate:
    name: str
    url: str
    updated_date: str
    latest_issue: str
    latest_issue_url: str
    note: str = ""

    @property
    def fingerprint(self) -> str:
        return f"{self.updated_date}|{self.latest_issue}|{self.latest_issue_url}"


@dataclass
class TargetSnapshot:
    latest: ChapterUpdate
    chapters: dict[int, ChapterUpdate] | None = None


class NotificationError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check configured sites and notify Discord on updates."
    )
    parser.add_argument(
        "--config-path",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the tracker JSON config.",
    )
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help="Path to the persisted state JSON file.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Discord delivery and print detected updates instead.",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> list[dict[str, Any]]:
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict) or "targets" not in data:
        raise ValueError(f"{config_path} must contain a JSON object with a targets array.")

    targets = data["targets"]
    if not isinstance(targets, list):
        raise ValueError(f"{config_path} targets field must be a JSON array.")

    normalized = []
    for item in targets:
        if not isinstance(item, dict) or "url" not in item or "type" not in item:
            raise ValueError("Each target must be an object with type and url fields.")
        normalized.append(item)
    return normalized


def load_state(state_path: Path) -> TrackerState:
    if not state_path.exists():
        return {}

    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(f"{state_path} must contain a JSON object.")
    return data


def save_state(state_path: Path, state: TrackerState) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handle = NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent, delete=False)
    temporary_path = Path(handle.name)
    try:
        with handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary_path.replace(state_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def fetch_html(url: str, timeout: int) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def clean_html_text(value: str) -> str:
    no_tags = TAG_RE.sub(" ", value)
    return WHITESPACE_RE.sub(" ", unescape(no_tags)).strip()


def parse_manhuagui_page(
    html: str, page_url: str, configured_name: str | None = None
) -> ChapterUpdate:
    status_match = STATUS_BLOCK_RE.search(html)
    if not status_match:
        raise ValueError(f"Could not find status block on {page_url}")

    status_block = status_match.group(1)

    date_match = DATE_RE.search(status_block)
    if not date_match:
        raise ValueError(f"Could not find updated date in status block for {page_url}")

    issue_match = ISSUE_RE.search(status_block)
    if not issue_match:
        raise ValueError(f"Could not find latest issue link in status block for {page_url}")

    title_match = TITLE_RE.search(html)
    page_title = clean_html_text(title_match.group(1)) if title_match else page_url

    return ChapterUpdate(
        name=configured_name or page_title,
        url=page_url,
        updated_date=clean_html_text(date_match.group(1)),
        latest_issue=clean_html_text(issue_match.group(2)),
        latest_issue_url=urljoin(MANHUAGUI_BASE_URL, issue_match.group(1)),
    )


def parse_comix_page(
    html: str, page_url: str, configured_name: str | None = None
) -> ChapterUpdate:
    initial_data = COMIX_INITIAL_DATA_RE.search(html)
    if not initial_data:
        raise ValueError(f"Could not find Comix title metadata on {page_url}")

    try:
        data = json.loads(initial_data.group(1))
        title_id = data["manga"]["hid"]
        detail_key = json.dumps(["manga", "detail", title_id], separators=(",", ":"))
        details = data["queries"][detail_key]
        title = details["title"]
        chapter = details["latestChapter"]
        chapter_url = details["latestChapterUrl"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid Comix title metadata on {page_url}") from exc

    if type(chapter) not in (int, float) or not isinstance(chapter_url, str) or not chapter_url:
        raise ValueError(f"Could not find latest chapter on {page_url}")

    return ChapterUpdate(
        name=configured_name or title,
        url=page_url,
        updated_date="",  # Comix exposes a relative age, which changes between checks.
        latest_issue=f"Chapter {chapter}",
        latest_issue_url=urljoin(page_url, chapter_url),
    )


def parse_manhuagui_chapters(html: str, latest: ChapterUpdate) -> dict[int, ChapterUpdate]:
    comic_path = urlsplit(latest.url).path.rstrip("/") + "/"
    chapters = {
        int(chapter_id): replace(
            latest,
            updated_date="",
            latest_issue=clean_html_text(title),
            latest_issue_url=urljoin(MANHUAGUI_BASE_URL, path),
        )
        for path, chapter_id, title in MANHUAGUI_CHAPTER_RE.findall(html)
        if path.startswith(comic_path)
    }
    if not chapters:
        raise ValueError(f"Could not find Manhuagui chapter list on {latest.url}")

    latest_id = int(Path(urlsplit(latest.latest_issue_url).path).stem)
    chapters[latest_id] = latest
    return chapters


def check_target(target: dict[str, Any], timeout: int) -> TargetSnapshot:
    target_type = target["type"]
    url = target["url"]
    configured_name = target.get("name")

    html = fetch_html(url, timeout=timeout)

    if target_type == "manhuagui":
        latest = parse_manhuagui_page(html, url, configured_name=configured_name)
        return TargetSnapshot(latest, parse_manhuagui_chapters(html, latest))
    if target_type == "comix":
        return TargetSnapshot(parse_comix_page(html, url, configured_name=configured_name))

    raise ValueError(f"Unsupported target type: {target_type}")


def manhuagui_updates(
    chapters: dict[int, ChapterUpdate], previous: dict[str, Any]
) -> tuple[list[ChapterUpdate], list[int]]:
    if not previous:
        return [], sorted(chapters)

    if "seen_chapter_ids" in previous:
        seen = set(previous["seen_chapter_ids"])
    else:
        # Old state stores only the most recent upload. Manhuagui IDs increase by upload.
        previous_id = int(Path(urlsplit(previous["latest_issue_url"]).path).stem)
        seen = {chapter_id for chapter_id in chapters if chapter_id <= previous_id}
        seen.add(previous_id)

    new_ids = sorted(chapters.keys() - seen)
    return [chapters[chapter_id] for chapter_id in new_ids], sorted(seen.union(chapters))


def comix_update(latest: ChapterUpdate, previous: dict[str, Any]) -> ChapterUpdate:
    before = COMIX_CHAPTER_NUMBER_RE.fullmatch(previous.get("latest_issue", ""))
    after = COMIX_CHAPTER_NUMBER_RE.fullmatch(latest.latest_issue)
    if before and after and Decimal(after[1]) - Decimal(before[1]) > 1:
        return replace(
            latest,
            note=f"Previously {before[0]}. Check the title page for chapters in between: {latest.url}",
        )
    return latest


def build_notification_lines(updates: list[ChapterUpdate]) -> list[str]:
    lines = ["New updates detected:"]
    for update in updates:
        date = f" ({update.updated_date})" if update.updated_date else ""
        lines.append(
            f"- {update.name}: {update.latest_issue}{date} {update.latest_issue_url}"
        )
        if update.note:
            lines.append(f"  {update.note}")
    return lines


def send_discord_notification(webhook_url: str, updates: list[ChapterUpdate]) -> None:
    content = "\n".join(build_notification_lines(updates))
    if len(content.encode("utf-16-le")) // 2 <= 2000:
        body = {"json": {"content": content}}
    else:
        summary = f"{len(updates)} new updates detected. See the attached chapter list."
        body = {
            "data": {"payload_json": json.dumps({"content": summary})},
            "files": {"files[0]": ("chapter-updates.txt", content.encode("utf-8"), "text/plain")},
        }
    try:
        response = requests.post(
            webhook_url,
            params={"wait": "true"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            **body,
        )
        response.raise_for_status()
        return
    except requests.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else "unknown"
        reason = response.reason if response is not None else "Unknown"
        response_body = response.text.strip() if response is not None else ""

        message = f"Discord webhook request failed with HTTP {status_code} {reason}."
        if response_body:
            message += f" Response body: {response_body}"
        if status_code == 403:
            message += (
                " This usually means the webhook URL is invalid, revoked, or no longer "
                "allowed to post to that channel."
            )
        raise NotificationError(message) from exc
    except requests.RequestException as exc:
        raise NotificationError(f"Discord webhook request failed: {exc}") from exc


def main() -> int:
    args = parse_args()
    config_path = Path(args.config_path)
    state_path = Path(args.state_path)

    targets = load_config(config_path)
    previous_state = load_state(state_path)
    next_state: TrackerState = {}
    updates: list[ChapterUpdate] = []
    checked_count = 0

    for i, target in enumerate(targets):
        url = target["url"]

        if i > 0:
            time.sleep(3)

        print(f"Checking {url}...", flush=True)
        previous = previous_state.get(url, {})

        try:
            snapshot = check_target(target, timeout=args.timeout)
            parsed = snapshot.latest
            if snapshot.chapters is not None:
                new_chapters, seen_ids = manhuagui_updates(snapshot.chapters, previous)
                updates.extend(new_chapters)
            elif previous and previous.get("fingerprint") != parsed.fingerprint:
                updates.append(comix_update(parsed, previous))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"Failed to check {url}: {exc}", file=sys.stderr, flush=True)
            if previous:
                next_state[url] = previous
            continue

        checked_count += 1
        next_state[url] = {
            "type": target["type"],
            "name": parsed.name,
            "updated_date": parsed.updated_date,
            "latest_issue": parsed.latest_issue,
            "latest_issue_url": parsed.latest_issue_url,
            "fingerprint": parsed.fingerprint,
            "checked_at": int(time.time()),
        }
        if snapshot.chapters is not None:
            next_state[url]["seen_chapter_ids"] = seen_ids

    if targets and not checked_count:
        return 1

    if updates and not args.dry_run:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            print(
                "Updates were detected, but DISCORD_WEBHOOK_URL is not set.",
                file=sys.stderr,
                flush=True,
            )
            return 1

        try:
            send_discord_notification(webhook_url, updates)
        except NotificationError as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 1

    save_state(state_path, next_state)

    if not previous_state:
        print("Seeded initial state without sending notifications.", flush=True)
        return 0

    if not updates:
        print("No updates detected.", flush=True)
        return 0

    if args.dry_run:
        print("\n".join(build_notification_lines(updates)), flush=True)
        print("Dry run enabled, skipping Discord delivery.", flush=True)
        return 0

    print(f"Sent Discord notification for {len(updates)} update(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
