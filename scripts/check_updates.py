#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
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
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ManhuaguiUpdate:
    name: str
    url: str
    updated_date: str
    latest_issue: str
    latest_issue_url: str

    @property
    def fingerprint(self) -> str:
        return f"{self.updated_date}|{self.latest_issue}|{self.latest_issue_url}"


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


def load_state(state_path: Path) -> dict[str, dict[str, str]]:
    if not state_path.exists():
        return {}

    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(f"{state_path} must contain a JSON object.")
    return data


def save_state(state_path: Path, state: dict[str, dict[str, str]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


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
) -> ManhuaguiUpdate:
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

    return ManhuaguiUpdate(
        name=configured_name or page_title,
        url=page_url,
        updated_date=clean_html_text(date_match.group(1)),
        latest_issue=clean_html_text(issue_match.group(2)),
        latest_issue_url=urljoin(MANHUAGUI_BASE_URL, issue_match.group(1)),
    )


def check_target(target: dict[str, Any], timeout: int) -> ManhuaguiUpdate:
    target_type = target["type"]
    url = target["url"]
    configured_name = target.get("name")

    html = fetch_html(url, timeout=timeout)

    if target_type == "manhuagui":
        return parse_manhuagui_page(html, url, configured_name=configured_name)

    raise ValueError(f"Unsupported target type: {target_type}")


def build_notification_lines(updates: list[ManhuaguiUpdate]) -> list[str]:
    lines = ["New updates detected:"]
    for update in updates:
        lines.append(
            f"- {update.name}: {update.latest_issue} ({update.updated_date}) {update.latest_issue_url}"
        )
    return lines


def send_discord_notification(webhook_url: str, updates: list[ManhuaguiUpdate]) -> None:
    payload = json.dumps(
        {"content": "\n".join(build_notification_lines(updates))},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20):
        return


def main() -> int:
    args = parse_args()
    config_path = Path(args.config_path)
    state_path = Path(args.state_path)

    targets = load_config(config_path)
    previous_state = load_state(state_path)
    next_state: dict[str, dict[str, str]] = {}
    updates: list[ManhuaguiUpdate] = []

    for target in targets:
        url = target["url"]
        print(f"Checking {url}...", flush=True)

        try:
            parsed = check_target(target, timeout=args.timeout)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"Failed to check {url}: {exc}", file=sys.stderr, flush=True)
            return 1

        next_state[url] = {
            "type": target["type"],
            "name": parsed.name,
            "updated_date": parsed.updated_date,
            "latest_issue": parsed.latest_issue,
            "latest_issue_url": parsed.latest_issue_url,
            "fingerprint": parsed.fingerprint,
            "checked_at": int(time.time()),
        }

        previous = previous_state.get(url)
        if previous and previous.get("fingerprint") != parsed.fingerprint:
            updates.append(parsed)

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

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(
            "Updates were detected, but DISCORD_WEBHOOK_URL is not set.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    send_discord_notification(webhook_url, updates)
    print(f"Sent Discord notification for {len(updates)} update(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

