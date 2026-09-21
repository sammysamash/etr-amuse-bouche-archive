#!/usr/bin/env python3
"""
Eat Talk Repeat - Amuse-Bouche archive builder.

Pulls sent Klaviyo email campaigns whose name contains "Amuse-Bouche",
keeps only those sent DELAY_DAYS (default 14) or more days ago, cleans the
HTML for public display, and writes:

    docs/index.json            list of public editions, newest first
    docs/editions/<slug>.html  one standalone page per edition

Editions already in docs/ are kept and not re-fetched, so the archive
survives even if a campaign is later deleted or archived in Klaviyo.

Environment variables:
    KLAVIYO_API_KEY   (required) private key, read-only Campaigns + Templates
    DELAY_DAYS        (default 14)
    NAME_MATCH        (default "amuse bouche"; hyphens/spaces/case ignored)
    SUBSCRIBE_URL     (optional) link for the "Subscribe" banner
    FORCE_REBUILD     (optional) "1" re-fetches every edition
    KLAVIYO_BASE      (testing only) override API base URL
"""
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

API_BASE = os.environ.get("KLAVIYO_BASE", "https://a.klaviyo.com/api")
REVISION = "2026-07-15"
DELAY_DAYS = int(os.environ.get("DELAY_DAYS", "14"))
NAME_MATCH = os.environ.get("NAME_MATCH", "amuse bouche")
SUBSCRIBE_URL = os.environ.get("SUBSCRIBE_URL", "").strip()
FORCE = os.environ.get("FORCE_REBUILD", "") == "1"

OUT = Path(__file__).parent / "docs"
EDITIONS = OUT / "editions"


# ---------------------------------------------------------------- Klaviyo API
def api_get(url):
    key = os.environ.get("KLAVIYO_API_KEY")
    if not key:
        sys.exit("KLAVIYO_API_KEY is not set.")
    if not url.startswith("http"):
        url = API_BASE + url
    req = urllib.request.Request(url, headers={
        "Authorization": f"Klaviyo-API-Key {key}",
        "revision": REVISION,
        "accept": "application/vnd.api+json",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            body = e.read().decode("utf-8", "replace")[:500]
            sys.exit(f"Klaviyo API error {e.code} for {url}\n{body}")
        except urllib.error.URLError:
            time.sleep(2 ** attempt)
    sys.exit(f"Klaviyo API kept failing for {url}")


def normalize(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def list_newsletter_campaigns():
    """All sent email campaigns whose name matches NAME_MATCH."""
    flt = "equals(messages.channel,'email')"
    url = "/campaigns?" + urllib.parse.urlencode(
        {"filter": flt, "include": "campaign-messages", "sort": "-created_at"})
    want = normalize(NAME_MATCH)
    found = []
    while url:
        page = api_get(url)
        messages = {i["id"]: i for i in page.get("included", [])
                    if i.get("type") == "campaign-message"}
        for c in page.get("data", []):
            a = c.get("attributes", {})
            if want not in normalize(a.get("name")):
                continue
            if (a.get("status") or "").lower() != "sent" or not a.get("send_time"):
                continue
            rel = (c.get("relationships", {}).get("campaign-messages", {})
                   .get("data") or [])
            msg = messages.get(rel[0]["id"]) if rel else None
            found.append((c, msg))
        url = (page.get("links") or {}).get("next")
    return found


def fetch_template_html(message_id):
    data = api_get(f"/campaign-messages/{message_id}?include=template")
    for inc in data.get("included", []):
        if inc.get("type") == "template":
            return inc.get("attributes", {}).get("html") or ""
    return ""


# ------------------------------------------------------------------- helpers
DATE_FORMATS = ["%Y-%m-%d", "%Y.%m.%d", "%m-%d-%Y", "%m/%d/%Y", "%m.%d.%Y",
                "%m-%d-%y", "%m/%d/%y", "%m.%d.%y",
                "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%b. %d, %Y"]


def issue_date(name, send_time):
    """Date from the campaign name (e.g. '9/11/26 Amuse-Bouche'); else send date."""
    head = re.split(r"amuse", name or "", flags=re.I)[0]
    head = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", head)
    head = head.strip(" \t-–—|:_")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            pass
    return send_time.date()


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


PRIVATE_LINK = re.compile(
    r"unsubscribe|manage[_ -]?(your[_ -]?)?preferences|update[_ -]?(your[_ -]?)?"
    r"(preferences|profile)|view[_ -]?(this[_ -]?)?(email[_ -]?)?in[_ -]?(your[_ -]?)?browser|"
    r"web[_ -]?view|forward[_ -]?to[_ -]?a[_ -]?friend", re.I)


# Klaviyo tags that render subscriber-only links, e.g. {% unsubscribe 'Unsubscribe' %}
PRIVATE_TAG = re.compile(
    r"\{%\s*(unsubscribe|unsubscribe_url|manage_preferences|manage_preferences_url|"
    r"web_view|web_view_url|forward_to_friend)\b[^%]*%\}", re.I)


def clean_html(raw, year=None, org_name=""):
    """Make a Klaviyo email template safe and sensible for the public web."""
    # Footer placeholders that have a sensible public value
    if year:
        raw = re.sub(r"\{%\s*current_year\s*%\}", str(year), raw)
    if org_name:
        raw = re.sub(r"\{\{\s*organization\.name[^}]*\}\}", htmllib.escape(org_name), raw)
    # Subscriber-only link tags, plus the " | " dividers and lead-in text around them
    raw = PRIVATE_TAG.sub("\x00", raw)
    raw = re.sub(r"Can(?:'|&#39;|&#x27;|\u2019|&rsquo;)t see this email\?\s*(?=\x00)", "", raw, flags=re.I)
    raw = re.sub(r"\s*\|\s*\x00|\x00\s*\|\s*", "\x00", raw)
    raw = raw.replace("\x00", "")

    soup = BeautifulSoup(raw, "html.parser")

    for tag in soup(["script", "noscript"]):
        tag.decompose()

    # Subscriber-only links (unsubscribe, preferences, view-in-browser)
    orphans = []
    for a in soup.find_all("a"):
        if PRIVATE_LINK.search(a.get("href", "")) or PRIVATE_LINK.search(a.get_text(" ")):
            holder = a.find_parent(["p", "td", "div", "span"])
            if holder is not None:
                orphans.append(holder)
            a.decompose()
    # Empty out leftovers such as "No longer want these emails? | "
    for holder in orphans:
        if holder.find(["a", "img", "table"]) is None and len(holder.get_text(" ").strip()) < 90:
            holder.clear()

    out = str(soup)
    # {{ first_name|default:'friend' }} -> friend ; other variables -> nothing
    out = re.sub(r"\{\{[^}]*?\|\s*default\s*:\s*['\"]([^'\"]*)['\"][^}]*\}\}", r"\1", out)
    # Removed names shouldn't leave a dangling space: "Hi {{ first_name }}," -> "Hi,"
    out = re.sub(r"\s*\{\{[^}]*\}\}\s*([,!.])", r"\1", out)
    out = re.sub(r"\{\{.*?\}\}", "", out, flags=re.S)
    out = re.sub(r"\{%.*?%\}", "", out, flags=re.S)
    return out


def clean_text(s):
    """Plain text for titles and previews: no tags, no personalization, no &amp;."""
    s = re.sub(r"\{\{[^}]*?\|\s*default\s*:\s*['\"]([^'\"]*)['\"][^}]*\}\}", r"\1", s or "")
    s = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|<[^>]+>", "", s, flags=re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def preview_text(cleaned, limit=180):
    soup = BeautifulSoup(cleaned, "html.parser")
    for hidden in soup.select('[style*="display:none"], [style*="display: none"]'):
        hidden.decompose()
    text = soup.get_text(" ")
    text = re.sub(r"[​‌‍­﻿͏]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Skip the masthead (nav links, date, "Vol 1, Issue 31") if present near the top
    m = re.search(r"Vol\.?\s*\d+\s*,\s*Issue\s*\d+\s*", text[:600], re.I)
    if m:
        text = text[m.end():]
    return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


RESIZE_SCRIPT = """<script>
(function(){function post(){parent.postMessage({etrHeight:document.documentElement.scrollHeight},'*');}
window.addEventListener('load',post);window.addEventListener('resize',post);
setTimeout(post,500);setTimeout(post,2000);})();
</script>"""


def wrap_page(cleaned, title):
    banner = ""
    if SUBSCRIBE_URL:
        banner = (
            '<div style="font-family:Helvetica,Arial,sans-serif;text-align:center;'
            'padding:14px 16px;background:#f6f1ea;color:#222;font-size:15px;">'
            "You're reading a past edition. "
            f'<a href="{htmllib.escape(SUBSCRIBE_URL)}" target="_top" '
            'style="color:#222;font-weight:bold;">Subscribe</a> '
            "to get the Amuse-Bouche two weeks earlier.</div>")
    head_extra = ('<meta name="viewport" content="width=device-width,initial-scale=1">'
                  '<base target="_blank">')
    if re.search(r"<body[^>]*>", cleaned, re.I):
        page = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, cleaned, 1, re.I)
        page = re.sub(r"</body>", RESIZE_SCRIPT + "</body>", page, 1, re.I)
        if re.search(r"<head[^>]*>", page, re.I):
            page = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + head_extra, page, 1, re.I)
        return page
    return (f"<!doctype html><html><head><meta charset='utf-8'>{head_extra}"
            f"<title>{htmllib.escape(title)}</title></head><body style='margin:0'>"
            f"{banner}{cleaned}{RESIZE_SCRIPT}</body></html>")


# ---------------------------------------------------------------------- main
def main():
    EDITIONS.mkdir(parents=True, exist_ok=True)
    index_path = OUT / "index.json"
    existing = {}
    if index_path.exists() and not FORCE:
        for e in json.loads(index_path.read_text())["editions"]:
            if (EDITIONS / f"{e['slug']}.html").exists():
                existing[e["id"]] = e

    cutoff = datetime.now(timezone.utc) - timedelta(days=DELAY_DAYS)
    editions = dict(existing)
    added, held = [], []

    for campaign, msg in list_newsletter_campaigns():
        cid = campaign["id"]
        a = campaign["attributes"]
        sent = parse_time(a["send_time"])
        if sent > cutoff:
            held.append(a["name"])
            continue
        if cid in editions:
            continue
        if not msg:
            print(f"  ! no message found for {a['name']}, skipping")
            continue
        m = msg.get("attributes", {})
        content = (m.get("definition") or {}).get("content") or m.get("content") or {}
        subject = clean_text(content.get("subject") or a["name"])
        raw = fetch_template_html(msg["id"])
        if not raw.strip():
            print(f"  ! empty template for {a['name']}, skipping")
            continue
        date = issue_date(a["name"], sent)
        cleaned = clean_html(raw, year=date.year, org_name=clean_text(content.get("from_label")))
        slug = date.isoformat()
        if any(e["slug"] == slug for e in editions.values()):
            slug = f"{slug}-{cid.lower()[:6]}"
        (EDITIONS / f"{slug}.html").write_text(wrap_page(cleaned, subject), encoding="utf-8")
        pre = clean_text(content.get("preview_text")) or preview_text(cleaned)
        editions[cid] = {"id": cid, "slug": slug, "title": subject,
                         "date": date.isoformat(), "preview": pre,
                         "path": f"editions/{slug}.html"}
        added.append(a["name"])

    ordered = sorted(editions.values(), key=lambda e: e["date"], reverse=True)
    new_body = {"editions": ordered}
    old_body = None
    if index_path.exists():
        old = json.loads(index_path.read_text())
        old_body = {"editions": old.get("editions")}
    if new_body != old_body:
        new_body["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        index_path.write_text(json.dumps(new_body, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / ".nojekyll").touch()

    print(f"Public editions: {len(ordered)}  |  newly added: {len(added)}  |  "
          f"held back (< {DELAY_DAYS} days old): {len(held)}")
    for n in added:
        print(f"  + {n}")
    for n in held:
        print(f"  (holding) {n}")


if __name__ == "__main__":
    main()
