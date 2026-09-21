#!/usr/bin/env python3
"""
Eat Talk Repeat - YouTube episodes list builder.

Reads the channel's public YouTube feed (no API key needed), skips Shorts,
keeps videos published on or after START_DATE, and writes

    docs/episodes.json   list of episodes, newest first

which the Code block on the website's Episodes page reads.

Episodes already in episodes.json are kept, so the list keeps growing even
though YouTube's feed only shows the most recent 15 videos. You can edit the
"caption" of any episode in episodes.json on GitHub and it will not be
overwritten.

Environment variables:
    YT_CHANNEL        (required) channel link, @handle or channel ID (UC...)
    START_DATE        (default 2026-09-18) ignore videos published before this
    TITLE_STYLE       "short" (default) or "full"
"""
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import os

CHANNEL = os.environ.get("YT_CHANNEL", "").strip()
START_DATE = date.fromisoformat(os.environ.get("START_DATE", "").strip() or "2026-09-18")
TITLE_STYLE = (os.environ.get("TITLE_STYLE", "").strip() or "short").lower()
LOCAL = ZoneInfo("America/Los_Angeles")

OUT = Path(__file__).parent / "docs" / "episodes.json"
NS = {"a": "http://www.w3.org/2005/Atom",
      "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
UA = {"User-Agent": "Mozilla/5.0 (compatible; ETR-episodes/1.0)",
      "Accept-Language": "en-US,en;q=0.9"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, follow=True):
    """Return (status, body). Retries on network trouble and 5xx/429."""
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    last = None
    for attempt in range(5):
        try:
            with opener.open(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and not follow:
                return e.code, ""
            if e.code == 429 or e.code >= 500 or (e.code == 404 and "feeds" in url):
                last = f"HTTP {e.code}"
                time.sleep(2 ** attempt * 3)
                continue
            return e.code, ""
        except urllib.error.URLError as e:
            last = str(e)
            time.sleep(2 ** attempt * 3)
    sys.exit(f"Could not reach {url} ({last}). YouTube may be having trouble; try Run workflow again later.")


def channel_id():
    m = re.search(r"(UC[\w-]{22})", CHANNEL)
    if m:
        return m.group(1)
    if not CHANNEL:
        sys.exit("YT_CHANNEL is not set. Add it under Settings > Secrets and variables > Actions > Variables.")
    handle = CHANNEL.rstrip("/").split("/")[-1]
    if not handle.startswith("@"):
        handle = "@" + handle
    status, page = fetch(f"https://www.youtube.com/{handle}")
    for pat in (r'"externalId":"(UC[\w-]{22})"', r'itemprop="identifier" content="(UC[\w-]{22})"',
                r'youtube\.com/channel/(UC[\w-]{22})', r'"channelId":"(UC[\w-]{22})"'):
        m = re.search(pat, page)
        if m:
            return m.group(1)
    sys.exit(f"Couldn't find the channel ID for {CHANNEL} (HTTP {status}). "
             "Set YT_CHANNEL to the channel ID instead: YouTube Studio > Settings > Channel > Advanced settings.")


def is_short(video_id, link):
    if "/shorts/" in (link or ""):
        return True
    # youtube.com/shorts/<id> answers 200 for a Short and redirects for a normal video
    status, _ = fetch(f"https://www.youtube.com/shorts/{video_id}", follow=False)
    return status == 200


def short_title(title):
    t = re.sub(r"\s*#\w+", "", title).strip()                     # drop hashtags
    t = re.sub(r"\s*[|\-–—]\s*Eat\.? ?Talk\.? ?Repeat\.?\s*$", "", t, flags=re.I).strip()
    if TITLE_STYLE == "full":
        return t
    m = re.split(r"\s*(?::|\s[|\-–—]\s)\s*", t, maxsplit=1)
    head = m[0].strip()
    return head if len(m) > 1 and len(head.split()) >= 3 else t


def main():
    cid = channel_id()
    status, xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    if status != 200:
        sys.exit(f"YouTube feed returned HTTP {status} for channel {cid}.")
    root = ET.fromstring(xml)

    existing = {}
    if OUT.exists():
        for e in json.loads(OUT.read_text())["episodes"]:
            existing[e["id"]] = e
    episodes = dict(existing)
    added, skipped_shorts = [], []

    for entry in root.findall("a:entry", NS):
        vid = entry.findtext("yt:videoId", namespaces=NS)
        title = htmllib.unescape(entry.findtext("a:title", default="", namespaces=NS)).strip()
        link_el = entry.find("a:link", NS)
        link = link_el.get("href") if link_el is not None else ""
        published = datetime.fromisoformat(entry.findtext("a:published", namespaces=NS))
        day = published.astimezone(LOCAL).date()
        if not vid or day < START_DATE:
            continue
        if vid in episodes:
            episodes[vid]["title"] = title          # pick up title edits made on YouTube
            continue
        if is_short(vid, link):
            skipped_shorts.append(title)
            continue
        episodes[vid] = {"id": vid, "date": day.isoformat(), "title": title,
                         "caption": short_title(title)}
        added.append(f"{day.isoformat()}  {title}")

    ordered = sorted(episodes.values(), key=lambda e: e["date"], reverse=True)
    body = {"episodes": ordered}
    old = json.loads(OUT.read_text()) if OUT.exists() else None
    if not old or old.get("episodes") != ordered:
        body["updated"] = datetime.now(LOCAL).isoformat(timespec="seconds")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT.parent / ".nojekyll").touch()

    print(f"Channel {cid}  |  episodes on the page: {len(ordered)}  |  newly added: {len(added)}")
    for a in added:
        print(f"  + {a}")
    for s in skipped_shorts:
        print(f"  (skipped Short) {s}")


if __name__ == "__main__":
    main()
