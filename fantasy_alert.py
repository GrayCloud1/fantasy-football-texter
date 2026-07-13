#!/usr/bin/env python3
"""
Fantasy Football Breaking News Texter
--------------------------------------
Polls r/fantasyfootball for new/hot posts, filters for likely "breaking news"
posts (keyword match OR fast-rising upvotes), and texts new matches to your
phone via your carrier's free email-to-SMS gateway.

Designed to be run on a schedule (e.g. every 5 min via GitHub Actions).
State (which posts have already been alerted) is kept in seen_posts.json,
which this script updates in place. Your workflow should commit that file
back to the repo after each run (see .github/workflows/alert.yml).
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

# ---------- Config ----------

SUBREDDIT = "fantasyfootball"
SEEN_FILE = Path(__file__).parent / "seen_posts.json"
MAX_SEEN_AGE_SECONDS = 60 * 60 * 12  # forget posts older than 12h to keep file small

# Keywords that usually indicate real breaking news (case-insensitive substring match)
KEYWORDS = [
    "out", "ruled out", "questionable", "doubtful", "ir", "injured reserve",
    "traded", "trade", "waived", "released", "signs", "signed", "activated",
    "elevated", "practice squad", "suspended", "suspension", "injury",
    "injured", "surgery", "torn", "fracture", "concussion", "placed on",
    "expected to miss", "will not play", "game-time decision", "designated",
    "starting", "will start", "benched", "demoted", "promoted", "claim",
]

# Fast-rising post thresholds: score this high within this many minutes = alert
VELOCITY_MIN_SCORE = 150
VELOCITY_MAX_AGE_MIN = 30

# ---------- Env vars (set these as GitHub Actions secrets) ----------

GMAIL_USER = os.environ.get("GMAIL_USER")           # your gmail address
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")  # gmail app password (not your login pw)
SMS_GATEWAY_ADDRESS = os.environ.get("SMS_GATEWAY_ADDRESS")  # e.g. 5551234567@vtext.com


def fetch_posts(listing="new", limit=25):
    url = f"https://www.reddit.com/r/{SUBREDDIT}/{listing}.json?limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return [child["data"] for child in data["data"]["children"]]


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    now = time.time()
    # prune old entries
    pruned = {pid: ts for pid, ts in seen.items() if now - ts < MAX_SEEN_AGE_SECONDS}
    SEEN_FILE.write_text(json.dumps(pruned))


def is_breaking(post):
    title = post.get("title", "").lower()
    keyword_hit = any(kw in title for kw in KEYWORDS)

    age_min = (time.time() - post.get("created_utc", time.time())) / 60
    velocity_hit = (
        post.get("score", 0) >= VELOCITY_MIN_SCORE and age_min <= VELOCITY_MAX_AGE_MIN
    )
    return keyword_hit or velocity_hit


def send_text(subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and SMS_GATEWAY_ADDRESS):
        print("Missing email/SMS config, skipping send. (Set GMAIL_USER, "
              "GMAIL_APP_PASSWORD, SMS_GATEWAY_ADDRESS as secrets.)")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = SMS_GATEWAY_ADDRESS

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [SMS_GATEWAY_ADDRESS], msg.as_string())


def main():
    seen = load_seen()
    alerts_sent = 0

    for listing in ("new", "hot"):
        try:
            posts = fetch_posts(listing=listing)
        except Exception as e:
            print(f"Failed to fetch {listing}: {e}")
            continue

        for post in posts:
            pid = post["id"]
            if pid in seen:
                continue
            if is_breaking(post):
                title = post.get("title", "")[:140]
                permalink = f"https://reddit.com{post.get('permalink', '')}"
                body = f"{title}\n{permalink}"
                print("ALERT:", body)
                send_text("FF News", body)
                alerts_sent += 1
            # mark seen regardless, so we don't re-check non-matching posts forever
            seen[pid] = time.time()

    save_seen(seen)
    print(f"Done. {alerts_sent} alert(s) sent.")


if __name__ == "__main__":
    sys.exit(main())
