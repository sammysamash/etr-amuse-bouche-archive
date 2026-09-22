#!/usr/bin/env python3
"""
Eat Talk Repeat - YouTube episodes list builder.

Reads the channel's videos from YouTube, skips Shorts, keeps videos published
on or after START_DATE, and writes

    docs/episodes.json   list of episodes, newest first

which the Code block on the website's Episodes page reads.

With YT_API_KEY set it uses the YouTube Data API and can reach every video on
the channel. Without it, it uses YouTube's public feed, which only shows the
most recent 15 videos. Episodes already in episodes.json are kept either way. You can edit the
"caption" of any episode in episodes.json on GitHub and it will not be
overwritten.

Environment variables:
    YT_CHANNEL        (required) channel link, @handle or channel ID (UC...)
    START_DATE        (default 2026-09-18) ignore videos published before this
    TITLE_STYLE       "short" (default) or "full"
    YT_API_KEY        (optional) YouTube Data API v3 key, for the full back catalog
"""
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import os

CHANNEL = os.environ.get("YT_CHANNEL", "").strip()
START_DATE = date.fromisoformat(os.environ.get("START_DATE", "").strip() or "2026-09-18")
TITLE_STYLE = (os.environ.get("TITLE_STYLE", "").strip() or "short").lower()
API_KEY = os.environ.get("YT_API_KEY", "").strip()
SHORTS_MAX_SECONDS = 180   # YouTube Shorts are 3 minutes or less
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


def feed_videos(cid):
    """Most recent 15 videos from the public feed: (id, title, published, is_short or None)."""
    status, xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    if status != 200:
        sys.exit(f"YouTube feed returned HTTP {status} for channel {cid}.")
    for entry in ET.fromstring(xml).findall("a:entry", NS):
        link_el = entry.find("a:link", NS)
        link = link_el.get("href") if link_el is not None else ""
        yield (entry.findtext("yt:videoId", namespaces=NS),
               htmllib.unescape(entry.findtext("a:title", default="", namespaces=NS)).strip(),
               datetime.fromisoformat(entry.findtext("a:published", namespaces=NS)),
               True if "/shorts/" in link else None)


def api_get(path, **params):
    params["key"] = API_KEY
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        sys.exit(f"YouTube API error {e.code}. Check the YT_API_KEY secret and that "
                 f"'YouTube Data API v3' is enabled for it.\n{detail}")


def seconds(iso):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def api_videos(cid):
    """Every public upload since START_DATE, via the YouTube Data API."""
    uploads = "UU" + cid[2:]
    ids, token = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = api_get("playlistItems", **params)
        stop = False
        for item in page.get("items", []):
            cd = item.get("contentDetails", {})
            when = cd.get("videoPublishedAt")
            if not when:
                continue
            if datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(LOCAL).date() < START_DATE:
                stop = True          # uploads are newest first, so everything after is older
                continue
            ids.append(cd["videoId"])
        token = page.get("nextPageToken")
        if stop or not token:
            break
    for i in range(0, len(ids), 50):
        page = api_get("videos", part="snippet,contentDetails,status", id=",".join(ids[i:i + 50]))
        for v in page.get("items", []):
            sn = v.get("snippet", {})
            if sn.get("liveBroadcastContent") in ("upcoming", "live"):
                continue
            if v.get("status", {}).get("privacyStatus") != "public":
                continue
            yield (v["id"], sn.get("title", "").strip(),
                   datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")),
                   seconds(v.get("contentDetails", {}).get("duration")) <= SHORTS_MAX_SECONDS)


def main():
    cid = channel_id()
    source = api_videos(cid) if API_KEY else feed_videos(cid)

    existing = {}
    if OUT.exists():
        for e in json.loads(OUT.read_text())["episodes"]:
            existing[e["id"]] = e
    episodes = dict(existing)
    added, skipped_shorts = [], []

    for vid, title, published, short in source:
        day = published.astimezone(LOCAL).date()
        if not vid or day < START_DATE:
            continue
        if vid in episodes:
            episodes[vid]["title"] = title          # pick up title edits made on YouTube
            continue
        if short if short is not None else is_short(vid, ""):
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

    print(f"Source: {'YouTube API (full catalog)' if API_KEY else 'public feed (latest 15 videos)'}")
    print(f"Channel {cid}  |  episodes on the page: {len(ordered)}  |  newly added: {len(added)}")
    for a in added:
        print(f"  + {a}")
    for s in skipped_shorts:
        print(f"  (skipped Short) {s}")


if __name__ == "__main__":
    main()
