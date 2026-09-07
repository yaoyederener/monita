#!/usr/bin/env python3
"""Monitor selected X accounts and forward new posts to Telegram."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


ACCOUNTS = ("HunterBiden", "Laptoptoken")
MONITOR_DAYS = 7
STATE_FILE = Path(__file__).resolve().parent / "data" / "state.json"
X_API_BASE = "https://api.x.com/2"
TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_TIMELINE_PAGES = 10


class MonitorError(RuntimeError):
    """A recoverable external-service or configuration failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MonitorError(f"Missing required environment variable: {name}")
    return value


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "started_at": None, "expires_at": None, "accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise MonitorError(f"Unsupported state format in {path}")
    data.setdefault("accounts", {})
    return data


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    error_label: str | None = None,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers or {})
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        label = error_label or url
        raise MonitorError(f"HTTP {exc.code} from {label}: {body[:800]}") from exc
    except urllib.error.URLError as exc:
        raise MonitorError(f"Network error for {error_label or url}: {exc.reason}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid JSON response from {error_label or url}") from exc
    if not isinstance(result, dict):
        raise MonitorError(f"Unexpected response from {error_label or url}")
    return result


def x_get(
    endpoint: str,
    bearer_token: str,
    params: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    url = f"{X_API_BASE}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    result = request_json(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "x-telegram-seven-day-monitor/1.0",
        },
    )
    if result.get("errors") and "data" not in result:
        raise MonitorError(f"X API error for {endpoint}: {result['errors']}")
    return result


def lookup_user_id(username: str, bearer_token: str) -> str:
    endpoint = f"/users/by/username/{urllib.parse.quote(username, safe='')}"
    result = x_get(endpoint, bearer_token)
    user_id = result.get("data", {}).get("id")
    if not user_id:
        raise MonitorError(f"X account @{username} was not found")
    return str(user_id)


def fetch_posts(
    user_id: str, bearer_token: str, since_id: str | None
) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    next_token: str | None = None

    for _ in range(MAX_TIMELINE_PAGES):
        params: dict[str, str | int] = {
            "max_results": 100,
            "tweet.fields": "created_at",
        }
        if since_id:
            params["since_id"] = since_id
        if next_token:
            params["pagination_token"] = next_token

        result = x_get(f"/users/{urllib.parse.quote(user_id, safe='')}/tweets", bearer_token, params)
        data = result.get("data", [])
        if data:
            posts.extend(item for item in data if isinstance(item, dict) and item.get("id"))
        next_token = result.get("meta", {}).get("next_token")
        # On the first run, the newest page is enough to establish a baseline:
        # older pages cannot contain a post created after started_at.
        if not next_token or not since_id:
            break
    else:
        raise MonitorError(
            f"More than {MAX_TIMELINE_PAGES * 100} posts appeared since the last check; "
            "increase MAX_TIMELINE_PAGES to guarantee full delivery"
        )

    unique = {str(post["id"]): post for post in posts}
    return sorted(unique.values(), key=lambda post: int(post["id"]))


def telegram_message(username: str, post: dict[str, Any]) -> str:
    text = str(post.get("text", "")).strip()
    link = f"https://x.com/{username}/status/{post['id']}"
    # Telegram sendMessage has a 4096-character limit; leave room for the header/link.
    if len(text) > 3600:
        text = text[:3599] + "…"
    return f"🔔 X 新推文\n@{username}\n\n{text}\n\n{link}"


def send_to_telegram(
    bot_token: str,
    chat_id: str,
    username: str,
    post: dict[str, Any],
    message_thread_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": telegram_message(username, post),
        "link_preview_options": {"is_disabled": False},
    }
    if message_thread_id:
        try:
            payload["message_thread_id"] = int(message_thread_id)
        except ValueError as exc:
            raise MonitorError("TELEGRAM_MESSAGE_THREAD_ID must be an integer") from exc

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    # Use a redacted log label because the Bot Token is part of Telegram's URL.
    result = request_json(url, payload=payload, error_label="Telegram Bot API")
    if not result.get("ok"):
        raise MonitorError(f"Telegram API rejected the message: {result}")


def monitor_account(
    username: str,
    account_state: dict[str, Any],
    *,
    bearer_token: str,
    bot_token: str,
    chat_id: str,
    message_thread_id: str | None,
    started_at: datetime,
    expires_at: datetime,
    checkpoint: Callable[[], None],
) -> int:
    user_id = account_state.get("user_id")
    if not user_id:
        user_id = lookup_user_id(username, bearer_token)
        account_state["user_id"] = user_id
        checkpoint()

    since_id = account_state.get("latest_post_id")
    posts = fetch_posts(str(user_id), bearer_token, str(since_id) if since_id else None)
    account_state["initialized"] = True
    checkpoint()

    sent = 0
    for post in posts:
        post_id = str(post["id"])
        created_at_raw = post.get("created_at")
        if not created_at_raw:
            raise MonitorError(f"Post {post_id} has no created_at timestamp")
        created_at = parse_time(str(created_at_raw))

        if started_at <= created_at < expires_at:
            send_to_telegram(
                bot_token,
                chat_id,
                username,
                post,
                message_thread_id,
            )
            sent += 1
            time.sleep(0.15)

        # Checkpoint only after a successful send (or after intentionally ignoring
        # a pre-window post), so a failed Telegram delivery is retried next run.
        account_state["latest_post_id"] = post_id
        checkpoint()

    return sent


def run(now: datetime | None = None, state_path: Path = STATE_FILE) -> int:
    state = load_state(state_path)
    current = now or utc_now()
    if state.get("completed"):
        print("Seven-day monitoring window is complete; nothing to do.")
        return 0

    bearer_token = require_env("X_BEARER_TOKEN")
    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    message_thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip() or None

    if not state.get("started_at"):
        state["started_at"] = isoformat_utc(current)
        state["expires_at"] = isoformat_utc(current + timedelta(days=MONITOR_DAYS))
        state["completed"] = False
        save_state(state, state_path)

    started_at = parse_time(state["started_at"])
    expires_at = parse_time(state["expires_at"])
    final_sweep = current >= expires_at

    total_sent = 0
    failures: list[str] = []
    accounts_state = state.setdefault("accounts", {})

    def checkpoint() -> None:
        save_state(state, state_path)

    for username in ACCOUNTS:
        account_state = accounts_state.setdefault(username, {})
        try:
            sent = monitor_account(
                username,
                account_state,
                bearer_token=bearer_token,
                bot_token=bot_token,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                started_at=started_at,
                expires_at=expires_at,
                checkpoint=checkpoint,
            )
            total_sent += sent
            print(f"@{username}: sent {sent} new post(s).")
        except MonitorError as exc:
            failures.append(f"@{username}: {exc}")
            print(f"ERROR @{username}: {exc}", file=sys.stderr)

    save_state(state, state_path)
    if final_sweep and not failures:
        state["completed"] = True
        save_state(state, state_path)
        print(f"Final sweep complete. Monitoring ended at {isoformat_utc(expires_at)}.")
    print(
        f"Monitoring {'finished' if final_sweep and not failures else 'active'}; "
        f"window ends at {isoformat_utc(expires_at)}. "
        f"Sent {total_sent} message(s) this run."
    )
    if failures:
        raise MonitorError("; ".join(failures))
    return total_sent


def main() -> int:
    try:
        run()
    except MonitorError as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
  
