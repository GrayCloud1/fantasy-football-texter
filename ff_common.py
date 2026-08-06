"""
Shared helpers for the fantasy football report scripts (daily + weekly).
Not used by fantasy_alert.py, which stays self-contained since it's the
most critical/live piece.
"""

import email.utils
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def send_notification(subject, body):
    if not NTFY_TOPIC:
        print("Missing NTFY_TOPIC secret, skipping send.")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": subject},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def player_name(p):
    return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()


def search_news_for_player(name, max_age_hours=48):
    """Find the most relevant recent news headline for a player via Google
    News RSS search. Returns None if nothing found, or if the best match is
    older than max_age_hours (avoids surfacing stale/unrelated articles)."""
    query = urllib.parse.quote(f"{name} NFL")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        raw = fetch_text(url)
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"News search failed for {name}: {e}")
        return None

    item = root.find(".//item")
    if item is None:
        return None

    title = item.findtext("title", default="")
    pub_date_raw = item.findtext("pubDate", default="")
    source_el = item.find("source")
    source = source_el.text if source_el is not None else None

    if pub_date_raw:
        try:
            pub_date = email.utils.parsedate_to_datetime(pub_date_raw)
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - pub_date).total_seconds() / 3600
            if age_hours > max_age_hours:
                return None
        except Exception:
            pass  # if we can't parse the date, don't block on it

    return {"title": title, "source": source}
