#!/usr/bin/env python3
"""Discord channel archiver.

Polls configured channels, appends new messages to per-channel .txt files,
and stores the last-seen message ID per channel in state.json so subsequent
runs are incremental. Skips threads and forum posts by design.

Env:
  DISCORD_BOT_TOKEN  required, bot token with Message Content intent
  DISCORD_CHANNELS   required, comma-separated channel IDs

Output:
  archives/<channel-name>.txt   appended to each run
  archives/state.json           {channel_id: last_message_id}
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://discord.com/api/v10"
ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "archives"
STATE_FILE = ARCHIVE_DIR / "state.json"
PAGE_SIZE = 100  # Discord max


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def discord_get(session: requests.Session, path: str, params: dict | None = None) -> list | dict:
    """GET with rate-limit handling. Discord returns 429 with retry_after seconds."""
    url = f"{API}{path}"
    while True:
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 429:
            retry_after = float(r.json().get("retry_after", 1.0))
            log(f"  rate limited, sleeping {retry_after:.1f}s")
            time.sleep(retry_after + 0.1)
            continue
        if r.status_code >= 500:
            log(f"  server error {r.status_code}, sleeping 5s and retrying")
            time.sleep(5)
            continue
        r.raise_for_status()
        # Be polite — stay well under the global 50 req/s limit.
        time.sleep(0.25)
        return r.json()


def get_channel_meta(session: requests.Session, channel_id: str) -> dict:
    return discord_get(session, f"/channels/{channel_id}")


def fetch_messages_after(session: requests.Session, channel_id: str, after_id: str | None) -> list:
    """Fetch ALL messages newer than after_id, oldest-first.

    Discord returns newest-first by default. With `after`, it returns oldest-first
    starting just after that ID. We page until we get fewer than PAGE_SIZE results.
    If after_id is None (first run), we fetch the entire history starting from the
    server epoch sentinel "0".
    """
    cursor = after_id or "0"
    collected: list = []
    while True:
        batch = discord_get(
            session,
            f"/channels/{channel_id}/messages",
            params={"after": cursor, "limit": PAGE_SIZE},
        )
        if not batch:
            break
        # API returns newest-first within the page; flip to oldest-first.
        batch.reverse()
        collected.extend(batch)
        cursor = batch[-1]["id"]
        log(f"  fetched {len(batch)} (total {len(collected)})")
        if len(batch) < PAGE_SIZE:
            break
    return collected


def format_message(msg: dict) -> str:
    """Render one message as a stable, append-only text block."""
    ts = msg.get("timestamp", "")
    author = msg.get("author") or {}
    username = author.get("username", "unknown")
    discriminator = author.get("discriminator", "0")
    handle = username if discriminator in ("0", "", None) else f"{username}#{discriminator}"
    content = msg.get("content", "") or ""

    lines = [f"[{ts}] {handle} (id={msg['id']})"]
    if content:
        for line in content.splitlines() or [""]:
            lines.append(f"    {line}")

    # Attachments — record URLs (they may rot, but at least we have a record).
    for att in msg.get("attachments") or []:
        lines.append(f"    [attachment] {att.get('filename', '')} {att.get('url', '')}")

    # Embeds — just note their presence + title/url if any.
    for emb in msg.get("embeds") or []:
        title = emb.get("title", "")
        url = emb.get("url", "")
        if title or url:
            lines.append(f"    [embed] {title} {url}".rstrip())

    # Stickers.
    for st in msg.get("sticker_items") or []:
        lines.append(f"    [sticker] {st.get('name', '')}")

    # Reply reference.
    ref = msg.get("referenced_message")
    if ref:
        ref_author = (ref.get("author") or {}).get("username", "?")
        lines.append(f"    [reply to {ref_author} msg={ref.get('id')}]")

    return "\n".join(lines) + "\n\n"


def safe_filename(name: str) -> str:
    keep = "-_."
    return "".join(c if c.isalnum() or c in keep else "-" for c in name).strip("-") or "channel"


def archive_text_channel(session: requests.Session, channel_id: str, name: str, state: dict) -> None:
    after = state.get(channel_id)
    log(f"  fetching after={after or '(beginning)'}")
    messages = fetch_messages_after(session, channel_id, after)

    if not messages:
        log("  no new messages")
        return

    out_path = ARCHIVE_DIR / f"{safe_filename(name)}-{datetime.now(timezone.utc):%Y-%m}.txt"
    is_new = not out_path.exists()
    with out_path.open("a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Archive of #{name} (channel id {channel_id})\n")
            f.write(f"# Shard started {datetime.now(timezone.utc).isoformat()}\n\n")
        for msg in messages:
            f.write(format_message(msg))

    state[channel_id] = messages[-1]["id"]
    log(f"  wrote {len(messages)} messages, last_id={state[channel_id]}")


def list_forum_threads(session: requests.Session, guild_id: str, forum_id: str) -> list[dict]:
    """Enumerate every thread under a forum: active + archived public.

    Active threads come from the guild-wide endpoint filtered by parent_id.
    Archived public threads are paginated via /threads/archived/public.
    """
    threads: dict[str, dict] = {}

    # Active threads (server-wide list, filter to this forum).
    try:
        active = discord_get(session, f"/guilds/{guild_id}/threads/active")
        for t in active.get("threads", []):
            if str(t.get("parent_id")) == str(forum_id):
                threads[t["id"]] = t
    except requests.HTTPError as e:
        log(f"  warning: active threads fetch failed: {e}")

    # Archived public threads, paginated by `before` (ISO timestamp of oldest seen).
    before: str | None = None
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        try:
            page = discord_get(session, f"/channels/{forum_id}/threads/archived/public", params=params)
        except requests.HTTPError as e:
            log(f"  warning: archived threads fetch failed: {e}")
            break
        page_threads = page.get("threads", [])
        for t in page_threads:
            threads[t["id"]] = t
        if not page.get("has_more") or not page_threads:
            break
        # API uses thread.archive_timestamp for the `before` cursor.
        meta = page_threads[-1].get("thread_metadata") or {}
        before = meta.get("archive_timestamp")
        if not before:
            break

    return list(threads.values())


def archive_forum_channel(session: requests.Session, channel_id: str, name: str, meta: dict, state: dict) -> None:
    guild_id = meta.get("guild_id")
    if not guild_id:
        log("  ERROR: forum has no guild_id, cannot enumerate active threads")
        return

    forum_dir = ARCHIVE_DIR / safe_filename(name)
    forum_dir.mkdir(parents=True, exist_ok=True)

    # Per-forum thread state lives nested under the forum's channel_id key.
    forum_state: dict = state.setdefault(channel_id, {})
    if not isinstance(forum_state, dict):
        # Safety: previous schema may have stored a string here.
        forum_state = {}
        state[channel_id] = forum_state

    threads = list_forum_threads(session, str(guild_id), channel_id)
    log(f"  found {len(threads)} threads in #{name}")

    total_msgs = 0
    for thread in threads:
        tid = thread["id"]
        tname = thread.get("name") or tid
        after = forum_state.get(tid)
        try:
            messages = fetch_messages_after(session, tid, after)
        except requests.HTTPError as e:
            log(f"    thread {tid} ({tname}) fetch failed: {e}")
            continue

        # The thread starter post in a forum has the SAME id as the thread itself
        # and is fetched via the messages endpoint normally — no special-case needed.
        if not messages:
            continue

        out_path = forum_dir / f"{tid}-{safe_filename(tname)}-{datetime.now(timezone.utc):%Y-%m}.txt"
        is_new = not out_path.exists()
        with out_path.open("a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# Thread '{tname}' in forum #{name}\n")
                f.write(f"# thread_id={tid} forum_id={channel_id}\n")
                f.write(f"# Shard started {datetime.now(timezone.utc).isoformat()}\n\n")
            for msg in messages:
                f.write(format_message(msg))

        forum_state[tid] = messages[-1]["id"]
        total_msgs += len(messages)
        log(f"    {tname[:40]}: +{len(messages)} (last_id={forum_state[tid]})")

    log(f"  forum #{name} total new messages: {total_msgs}")


def archive_channel(session: requests.Session, channel_id: str, state: dict) -> None:
    meta = get_channel_meta(session, channel_id)
    name = meta.get("name") or channel_id
    ctype = meta.get("type")
    log(f"channel #{name} ({channel_id}) type={ctype}")

    # 0 = GUILD_TEXT, 5 = GUILD_ANNOUNCEMENT, 15 = GUILD_FORUM, 16 = GUILD_MEDIA (forum-like)
    if ctype in (0, 5):
        archive_text_channel(session, channel_id, name, state)
    elif ctype in (15, 16):
        archive_forum_channel(session, channel_id, name, meta, state)
    else:
        log(f"  skipping unsupported channel type {ctype}")
        return


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channels_env = os.environ.get("DISCORD_CHANNELS", "")
    if not token:
        log("ERROR: DISCORD_BOT_TOKEN not set")
        return 1
    channel_ids = [c.strip() for c in channels_env.split(",") if c.strip()]
    if not channel_ids:
        log("ERROR: DISCORD_CHANNELS not set (comma-separated channel IDs)")
        return 1

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bot {token}",
        "User-Agent": "NousArchiveBot (https://github.com/NousResearch, 1.0)",
    })

    failures = 0
    for cid in channel_ids:
        try:
            archive_channel(session, cid, state)
            save_state(state)  # checkpoint after each channel
        except requests.HTTPError as e:
            failures += 1
            log(f"  HTTPError on {cid}: {e} body={e.response.text[:300] if e.response else ''}")
        except Exception as e:
            failures += 1
            log(f"  ERROR on {cid}: {e!r}")

    log(f"done. failures={failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
