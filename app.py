"""
HEOC News Monitor
-----------------
Scans news sources (RSS or HTML listing pages) for health-related keywords,
saves matching articles as text files and writes a meta CSV.

Install:   pip install streamlit requests beautifulsoup4 feedparser pandas trafilatura
Run UI:    streamlit run heoc_news_monitor.py
Run CLI:   python heoc_news_monitor.py --cli      (for Task Scheduler / cron)
"""
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # optional, falls back to <p> tags
    trafilatura = None

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "heoc_config.json"
SEEN_FILE = BASE / "seen_urls.txt"
RUNS_DIR = BASE / "runs"
HEADERS = {"User-Agent": "HEOC-NewsMonitor/1.0 (health surveillance; contact: your-email@example.com)"}

# NOTE: URLs are examples. Verify each one and edit in the UI.
DEFAULT_CONFIG = {
    "sources": [
        {"name": "Kathmandu Post - Health", "url": "https://kathmandupost.com/health", "type": "html", "link_contains": "/health/"},
        {"name": "Onlinekhabar English", "url": "https://english.onlinekhabar.com/feed", "type": "rss", "link_contains": ""},
        {"name": "Republica", "url": "https://myrepublica.nagariknetwork.com/feed", "type": "rss", "link_contains": ""},
        {"name": "Setopati", "url": "https://www.setopati.com/feed", "type": "rss", "link_contains": ""},
        {"name": "EDCD", "url": "https://edcd.gov.np", "type": "html", "link_contains": ""},
    ],
    "keywords": [
        "dengue", "cholera", "typhoid", "diarrhoea", "outbreak", "epidemic",
        "covid", "influenza", "measles", "rabies", "flood", "landslide", "earthquake",
        "डेंगु", "हैजा", "टाइफाइड", "झाडापखाला", "महामारी", "प्रकोप", "कोरोना", "इन्फ्लुएन्जा", "दादुरा", "बाढी", "पहिरो", "भूकम्प",
    ],
    "days_back": 2,
    "fetch_full_text": True,
    "max_per_source": 40,
    "delay_sec": 1.0,
    "skip_seen": True,
}


# ---------- config ----------
def load_config():
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- fetching ----------
def get(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding  # important for Nepali pages
    return r


def fetch_rss(src):
    feed = feedparser.parse(get(src["url"]).content)
    items = []
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6])
        summary = BeautifulSoup(getattr(e, "summary", ""), "html.parser").get_text(" ")
        items.append({"title": e.get("title", ""), "url": e.get("link", ""), "published": pub, "summary": summary})
    return items


def fetch_html_listing(src):
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")
    domain = urlparse(src["url"]).netloc
    must = src.get("link_contains", "")
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        link = urljoin(src["url"], a["href"]).split("#")[0]
        title = a.get_text(" ", strip=True)
        if urlparse(link).netloc != domain or link in seen or len(title) < 20:
            continue
        if must and must not in link:
            continue
        seen.add(link)
        items.append({"title": title, "url": link, "published": None, "summary": ""})
    return items


def fetch_article(url):
    """Return (text, published_datetime_or_None)."""
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    pub = None
    meta = soup.find("meta", property="article:published_time")
    if meta and meta.get("content"):
        try:
            pub = datetime.fromisoformat(meta["content"].replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    text = trafilatura.extract(html) if trafilatura else None
    if not text:
        text = "\n".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    return text or "", pub


# ---------- matching ----------
def find_matches(text, keywords):
    t = text.lower()
    return [k for k in keywords if k.lower() in t]


def slugify(s):
    return re.sub(r"[^\w]+", "_", s, flags=re.UNICODE).strip("_")[:60] or "article"


def run_scan(cfg, log=print):
    keywords = [k.strip() for k in cfg["keywords"] if k.strip()]
    cutoff = datetime.now() - timedelta(days=cfg["days_back"])
    seen = set(SEEN_FILE.read_text(encoding="utf-8").split()) if SEEN_FILE.exists() else set()
    out_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / "articles").mkdir(parents=True, exist_ok=True)
    rows, new_seen = [], set()

    for src in cfg["sources"]:
        name = src["name"]
        try:
            items = fetch_rss(src) if src["type"] == "rss" else fetch_html_listing(src)
        except Exception as ex:
            log(f"❌ {name}: {ex}")
            continue
        items = items[: int(cfg["max_per_source"])]
        log(f"🔎 {name}: {len(items)} candidates")
        for it in items:
            url = it["url"]
            if not url or (cfg["skip_seen"] and url in seen):
                continue
            if it["published"] and it["published"] < cutoff:
                continue
            matched = find_matches(it["title"] + " " + it["summary"], keywords)
            text, pub = "", it["published"]
            if cfg["fetch_full_text"] or not matched:
                if not matched and not cfg["fetch_full_text"]:
                    continue
                try:
                    text, art_pub = fetch_article(url)
                    pub = pub or art_pub
                    time.sleep(cfg["delay_sec"])
                except Exception as ex:
                    log(f"   ⚠️ could not open {url}: {ex}")
                matched = find_matches(it["title"] + " " + it["summary"] + " " + text, keywords)
            if not matched:
                continue
            if pub and pub < cutoff:
                continue
            fname = f"{len(rows)+1:03d}_{slugify(name)}_{slugify(it['title'])}.txt"
            (out_dir / "articles" / fname).write_text(
                f"Title: {it['title']}\nSource: {name}\nURL: {url}\nPublished: {pub}\n"
                f"Keywords: {', '.join(matched)}\n\n{text or it['summary']}", encoding="utf-8")
            rows.append({
                "source": name, "title": it["title"], "url": url,
                "published_date": pub.date() if pub else "", "published_time": pub.time() if pub else "",
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "matched_keywords": ", ".join(matched), "file": fname,
            })
            new_seen.add(url)
            log(f"   ✅ {it['title'][:80]}  [{', '.join(matched)}]")

    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "meta.csv", index=False, encoding="utf-8-sig")  # utf-8-sig opens Nepali correctly in Excel
    if new_seen:
        with SEEN_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(new_seen) + "\n")
    return df, out_dir


def make_zip(out_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in out_dir.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(out_dir))
    return buf.getvalue()


# ---------- UI ----------
def main_ui():
    import streamlit as st

    st.set_page_config(page_title="HEOC News Monitor", page_icon="🩺", layout="wide")
    st.title("🩺 HEOC News Monitor")
    st.caption("Scan national/local news sources for health keywords and export articles + meta CSV.")

    if "cfg" not in st.session_state:
        st.session_state.cfg = load_config()
    cfg = st.session_state.cfg

    with st.sidebar:
        st.header("Settings")
        cfg["days_back"] = st.number_input("Look back (days)", 1, 30, cfg["days_back"])
        cfg["max_per_source"] = st.number_input("Max articles checked per source", 5, 200, cfg["max_per_source"])
        cfg["fetch_full_text"] = st.checkbox("Open each article & search full text (slower, more accurate)", cfg["fetch_full_text"])
        cfg["skip_seen"] = st.checkbox("Skip links found in earlier runs", cfg["skip_seen"])
        cfg["delay_sec"] = st.slider("Delay between requests (sec)", 0.0, 5.0, float(cfg["delay_sec"]), 0.5)

    st.subheader("1. Sources")
    st.caption("type = `rss` (feed URL) or `html` (listing page). `link_contains` optionally limits HTML links, e.g. `/health/`.")
    sources_df = st.data_editor(
        pd.DataFrame(cfg["sources"]), num_rows="dynamic", use_container_width=True,
        column_config={"type": st.column_config.SelectboxColumn("type", options=["rss", "html"], required=True)},
    )
    st.subheader("2. Keywords")
    kw_text = st.text_area("One per line or comma-separated (English and Nepali)", "\n".join(cfg["keywords"]), height=150)

    c1, c2 = st.columns(2)
    cfg["sources"] = sources_df.fillna("").to_dict("records")
    cfg["keywords"] = [k.strip() for k in re.split(r"[,\n]", kw_text) if k.strip()]
    if c1.button("💾 Save settings"):
        save_config(cfg)
        st.success("Saved to heoc_config.json")

    if c2.button("▶️ Run scan", type="primary"):
        save_config(cfg)
        box, lines = st.empty(), []

        def log(msg):
            lines.append(msg)
            box.code("\n".join(lines[-15:]))

        with st.spinner("Scanning..."):
            df, out_dir = run_scan(cfg, log)
        st.session_state.result = (df, make_zip(out_dir), out_dir.name)

    if "result" in st.session_state:
        df, zip_bytes, name = st.session_state.result
        st.subheader(f"Results: {len(df)} matching articles")
        if len(df):
            st.dataframe(df, use_container_width=True)
            d1, d2 = st.columns(2)
            d1.download_button("⬇️ Meta CSV", df.to_csv(index=False, encoding="utf-8-sig"), f"meta_{name}.csv", "text/csv")
            d2.download_button("⬇️ All (articles + CSV) .zip", zip_bytes, f"heoc_news_{name}.zip", "application/zip")
        else:
            st.info("Nothing new matched. Try more days, more keywords, or turn off 'skip seen links'.")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        df, d = run_scan(load_config())
        print(f"Done: {len(df)} articles saved in {d}")
    else:
        main_ui()
