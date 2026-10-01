"""
HEOC News Monitor v2
Run UI:  streamlit run app.py
Run CLI: python app.py --cli        (for scheduled runs)
Packages: streamlit requests beautifulsoup4 feedparser pandas trafilatura
"""
import calendar
import io
import json
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:
    trafilatura = None

NPT = timezone(timedelta(hours=5, minutes=45))  # Nepal Time, no extra package needed
BASE = Path(__file__).parent
CONFIG_FILE, SEEN_FILE, RUNS_DIR = BASE / "heoc_config.json", BASE / "seen_urls.txt", BASE / "runs"
HEADERS = {"User-Agent": "HEOC-NewsMonitor/2.0 (health surveillance; contact: your-email@example.com)"}
TIERS = ["Critical", "Watch", "Routine"]
ICON = {"Critical": "🔴 Critical", "Watch": "🟠 Watch", "Routine": "🟢 Routine"}
COLS = ["enabled", "name", "category", "type", "url", "link_contains", "fulltext"]


def now():
    return datetime.now(NPT)


def S(name, url, typ="rss", cat="National", contains="", full=True, on=True):
    return dict(enabled=on, name=name, url=url, type=typ, category=cat, link_contains=contains, fulltext=full)


# URLs are starting points. Use "Test sources" in the app to verify, then fix or switch off failing ones.
DEFAULTS = {
    "sources": [
        S("Kathmandu Post - Health", "https://kathmandupost.com/health", "html", contains="/health/"),
        S("Kathmandu Post - Feed", "https://kathmandupost.com/rss"),
        S("Onlinekhabar English", "https://english.onlinekhabar.com/feed"),
        S("Onlinekhabar (Nepali)", "https://www.onlinekhabar.com/feed"),
        S("Setopati (Nepali)", "https://www.setopati.com/feed"),
        S("Ratopati (Nepali)", "https://www.ratopati.com/feed"),
        S("Khabarhub", "https://khabarhub.com/feed"),
        S("Himalayan Times", "https://thehimalayantimes.com/feed"),
        S("Republica", "https://myrepublica.nagariknetwork.com/feed"),
        S("eKantipur", "https://ekantipur.com/rss"),
        S("Gorkhapatra", "https://gorkhapatraonline.com", "html"),
        S("Annapurna Post", "https://annapurnapost.com", "html"),
        S("HEOC / MoHP", "https://heoc.mohp.gov.np", "html", "Government"),
        S("MoHP", "https://mohp.gov.np", "html", "Government"),
        S("EDCD", "https://edcd.gov.np", "html", "Government"),
        S("Google News - English", "https://news.google.com/rss/search?q=Nepal+outbreak+OR+dengue+OR+cholera+OR+epidemic+when:1d&hl=en-NP&gl=NP&ceid=NP:en", cat="Aggregator", full=False),
        S("Google News - Nepali", "https://news.google.com/rss/search?q=डेंगु+OR+हैजा+OR+प्रकोप+OR+महामारी+when:1d&hl=ne&gl=NP&ceid=NP:ne", cat="Aggregator", full=False),
        S("Example local portal (edit me)", "https://example.com/feed", cat="Local", on=False),
    ],
    "keywords": {
        "Critical": ["outbreak", "epidemic", "cholera", "dengue", "food poisoning", "प्रकोप", "महामारी", "हैजा", "डेंगु", "डेंगू", "खाद्य विषाक्तता"],
        "Watch": ["typhoid", "diarrhoea", "influenza", "measles", "rabies", "covid", "scrub typhus", "टाइफाइड", "झाडापखाला", "इन्फ्लुएन्जा", "दादुरा", "रेबिज", "कोरोना", "कोभिड"],
        "Routine": ["flood", "landslide", "earthquake", "बाढी", "पहिरो", "भूकम्प"],
    },
    "window_hours": 24, "fetch_full_text": True, "max_per_source": 40,
    "delay_sec": 1.0, "skip_seen": True, "workers": 4,
}


# ---------- config ----------
def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved.get("keywords"), list):  # old format
                saved["keywords"] = {"Critical": [], "Watch": saved["keywords"], "Routine": []}
            if "days_back" in saved and "window_hours" not in saved:
                saved["window_hours"] = int(saved["days_back"]) * 24
            cfg.update({k: v for k, v in saved.items() if k in cfg})
        except Exception:
            pass
    cfg["sources"] = [{**S("", ""), **s} for s in cfg["sources"]]
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- fetching ----------
def short(ex):
    return f"{type(ex).__name__}: {str(ex)[:110]}"


def to_npt(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=NPT) if dt.tzinfo is None else dt.astimezone(NPT)


def get(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"  # keeps Nepali text readable
    return r


def fetch_rss(src):
    feed = feedparser.parse(get(src["url"]).content)
    if not feed.entries:
        raise ValueError("No entries found - not a valid RSS feed?")
    items = []
    for e in feed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime.fromtimestamp(calendar.timegm(t), NPT) if t else None
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
        items.append({"title": e.get("title", ""), "url": e.get("link", ""), "published": pub, "summary": summary})
    return items


def fetch_html_listing(src):
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")
    domain, must, seen, items = urlparse(src["url"]).netloc, src.get("link_contains", ""), set(), []
    for a in soup.find_all("a", href=True):
        link = urljoin(src["url"], a["href"]).split("#")[0]
        title = a.get_text(" ", strip=True)
        if urlparse(link).netloc != domain or link in seen or len(title) < 20 or (must and must not in link):
            continue
        seen.add(link)
        items.append({"title": title, "url": link, "published": None, "summary": ""})
    if not items:
        raise ValueError("No article links found (site may need JavaScript)")
    return items


def fetch_article(url):
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    pub = None
    for tag in (soup.find("meta", attrs={"property": "article:published_time"}),
                soup.find("meta", attrs={"name": "article:published_time"}),
                soup.find("meta", attrs={"itemprop": "datePublished"}),
                soup.find("time", attrs={"datetime": True})):
        if tag is None:
            continue
        try:
            pub = to_npt(datetime.fromisoformat((tag.get("content") or tag.get("datetime")).strip().replace("Z", "+00:00")))
            break
        except Exception:
            continue
    text = trafilatura.extract(html) if trafilatura else None
    if not text:
        text = "\n".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    return text or "", pub


# ---------- scanning ----------
def find_matches(text, kw):
    t = text.lower()
    for tier in TIERS:  # highest tier that matches decides priority
        hits = [k for k in kw[tier] if k.lower() in t]
        if hits:
            return tier, hits + [k for tt in TIERS[TIERS.index(tier) + 1:] for k in kw[tt] if k.lower() in t]
    return None, []


def scan_source(src, cfg, cutoff, seen, kw):
    st = dict(source=src["name"], category=src.get("category", ""), status="OK", checked=0, matches=0, note="")
    hits = []
    try:
        items = (fetch_rss(src) if src["type"] == "rss" else fetch_html_listing(src))[: int(cfg["max_per_source"])]
        st["checked"] = len(items)
        for it in items:
            url, pub = it["url"], it["published"]
            if not url or url in seen or (pub and pub < cutoff):
                continue
            head, text = it["title"] + " " + it["summary"], ""
            if cfg["fetch_full_text"] and src.get("fulltext", True):
                try:
                    text, art_pub = fetch_article(url)
                    pub = pub or art_pub
                    time.sleep(cfg["delay_sec"])
                except Exception:
                    pass
                if pub and pub < cutoff:
                    continue
            prio, matched = find_matches(head + " " + text, kw)
            if prio:
                hits.append(dict(priority=prio, source=src["name"], category=src.get("category", ""), title=it["title"],
                                 url=url, published=pub, text=text or it["summary"], matched=matched))
        st["matches"] = len(hits)
    except Exception as ex:
        st.update(status="FAILED", note=short(ex))
    return st, hits


def run_scan(cfg, progress=None):
    kw = {t: [k.strip() for k in cfg["keywords"].get(t, []) if k.strip()] for t in TIERS}
    cutoff = now() - timedelta(hours=float(cfg["window_hours"]))
    seen = set(SEEN_FILE.read_text(encoding="utf-8").split()) if (cfg["skip_seen"] and SEEN_FILE.exists()) else set()
    sources = [s for s in cfg["sources"] if s.get("enabled") and s.get("url")]
    out_dir = RUNS_DIR / now().strftime("%Y%m%d_%H%M%S")
    (out_dir / "articles").mkdir(parents=True, exist_ok=True)

    statuses, hits = [], []
    with ThreadPoolExecutor(max_workers=int(cfg["workers"])) as ex:
        futs = [ex.submit(scan_source, s, cfg, cutoff, seen, kw) for s in sources]
        for i, f in enumerate(as_completed(futs), 1):
            st, h = f.result()
            statuses.append(st)
            hits += h
            if progress:
                progress(i / len(futs), st["source"])

    uniq = {h["url"]: h for h in hits}.values()
    ordered = sorted(uniq, key=lambda h: (TIERS.index(h["priority"]), -(h["published"].timestamp() if h["published"] else 0)))
    rows = []
    for n, h in enumerate(ordered, 1):
        fname = f"{n:03d}_{re.sub(r'[^\w]+', '_', h['source'], flags=re.UNICODE)[:25]}_{re.sub(r'[^\w]+', '_', h['title'], flags=re.UNICODE)[:50]}.txt"
        pub = h["published"].strftime("%Y-%m-%d %H:%M") if h["published"] else ""
        (out_dir / "articles" / fname).write_text(
            f"Title: {h['title']}\nSource: {h['source']}\nURL: {h['url']}\nPublished (NPT): {pub}\n"
            f"Priority: {h['priority']}\nKeywords: {', '.join(h['matched'])}\n\n{h['text']}", encoding="utf-8")
        rows.append(dict(priority=h["priority"], source=h["source"], category=h["category"], title=h["title"], url=h["url"],
                         published_npt=pub, date_note="" if pub else "date not found - please verify",
                         matched_keywords=", ".join(h["matched"]), fetched_at=now().strftime("%Y-%m-%d %H:%M:%S"), file=fname))
    df, sdf = pd.DataFrame(rows), pd.DataFrame(statuses)
    df.to_csv(out_dir / "meta.csv", index=False, encoding="utf-8-sig")
    sdf.to_csv(out_dir / "source_status.csv", index=False, encoding="utf-8-sig")
    if rows:
        with SEEN_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(r["url"] for r in rows) + "\n")
    return df, sdf, out_dir


def test_sources(cfg):
    def one(s):
        try:
            items = fetch_rss(s) if s["type"] == "rss" else fetch_html_listing(s)
            return dict(source=s["name"], status="OK", items_found=len(items), sample=items[0]["title"][:70])
        except Exception as ex:
            return dict(source=s["name"], status="FAILED", items_found=0, sample=short(ex))
    srcs = [s for s in cfg["sources"] if s.get("enabled") and s.get("url")]
    with ThreadPoolExecutor(max_workers=6) as ex:
        return pd.DataFrame(list(ex.map(one, srcs)))


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
    try:  # optional password: add APP_PASSWORD in Streamlit secrets or .streamlit/secrets.toml
        pw = st.secrets.get("APP_PASSWORD")
    except Exception:
        pw = None
    if pw and st.text_input("Password", type="password") != pw:
        st.stop()

    st.markdown("""<style>
    .hdr{background:linear-gradient(90deg,#0b3c5d,#1d6fa5);padding:18px 24px;border-radius:10px;color:white;margin-bottom:12px}
    .hdr h2{margin:0;color:white}.hdr p{margin:2px 0 0;opacity:.85}
    </style><div class="hdr"><h2>🩺 Health Emergency Operation Centre - News Monitor</h2>
    <p>Automatic scan of national and local news for health alerts</p></div>""", unsafe_allow_html=True)

    if "cfg" not in st.session_state:
        st.session_state.cfg = load_config()
        st.session_state.src_df = pd.DataFrame(st.session_state.cfg["sources"])[COLS]
    cfg = st.session_state.cfg

    # sidebar
    with st.sidebar:
        st.header("⚙️ Scan settings")
        presets = {"Last 2 hours": 2, "Last 6 hours": 6, "Last 12 hours": 12, "Last 24 hours": 24,
                   "Last 48 hours": 48, "Last 7 days": 168, "Custom (hours)": None}
        label = st.selectbox("Time window", list(presets), index=3)
        hours = presets[label] or st.number_input("Hours", 1, 720, 24)
        cfg["window_hours"] = hours
        cfg["skip_seen"] = st.checkbox("Hide articles found in earlier scans", cfg["skip_seen"])
        cfg["fetch_full_text"] = st.checkbox("Read full article text (slower, more accurate)", cfg["fetch_full_text"])
        with st.expander("Advanced"):
            cfg["max_per_source"] = st.number_input("Max articles per source", 5, 200, int(cfg["max_per_source"]))
            cfg["delay_sec"] = st.slider("Delay between requests (sec)", 0.0, 5.0, float(cfg["delay_sec"]), 0.5)
            cfg["workers"] = st.slider("Sources scanned in parallel", 1, 8, int(cfg["workers"]))
        if st.button("💾 Save settings"):
            save_config(cfg)
            st.success("Saved")
        if st.button("↩️ Reset to defaults"):
            if CONFIG_FILE.exists():
                CONFIG_FILE.unlink()
            for k in ("cfg", "src_df", "result"):
                st.session_state.pop(k, None)
            st.rerun()

    t_dash, t_src, t_kw, t_hist, t_help = st.tabs(["🏠 Dashboard", "📰 Sources", "🔑 Keywords", "🗂 History", "❓ Help"])

    with t_src:
        st.caption("Tick/untick **On** to include a source. Add rows at the bottom. Type: `rss` = feed link, `html` = ordinary web page.")
        edited = st.data_editor(
            st.session_state.src_df, num_rows="dynamic", key="src_editor",
            column_config={
                "enabled": st.column_config.CheckboxColumn("On", default=True),
                "name": st.column_config.TextColumn("Name", required=True),
                "category": st.column_config.SelectboxColumn("Category", options=["National", "Local", "Government", "Aggregator"], default="Local"),
                "type": st.column_config.SelectboxColumn("Type", options=["rss", "html"], default="rss", required=True),
                "url": st.column_config.TextColumn("URL / feed link", required=True),
                "link_contains": st.column_config.TextColumn("Link must contain"),
                "fulltext": st.column_config.CheckboxColumn("Read full text", default=True),
            })
        e = edited.fillna({"enabled": True, "fulltext": True, "type": "rss", "category": "Local", "name": "", "url": "", "link_contains": ""})
        cfg["sources"] = [r for r in e.to_dict("records") if str(r["url"]).strip()]
        if st.button("🧪 Test all sources"):
            with st.spinner("Checking each source..."):
                res = test_sources(cfg)
            st.dataframe(res, hide_index=True)
            st.caption("FAILED = wrong link, site blocking us, or page needs JavaScript. Fix the link or switch it off.")

    with t_kw:
        st.caption("One keyword per line, English or Nepali. An article's priority is the highest level it matches.")
        cols, kw = st.columns(3), {}
        for c, tier, hint in zip(cols, TIERS, ["Act first", "Keep an eye on", "Background"]):
            with c:
                txt = st.text_area(f"{ICON[tier]} ({hint})", "\n".join(cfg["keywords"].get(tier, [])), height=320, key=f"kw_{tier}")
                kw[tier] = [k.strip() for k in txt.split("\n") if k.strip()]
        cfg["keywords"] = kw

    with t_dash:
        n_src = sum(1 for s in cfg["sources"] if s.get("enabled"))
        since = (now() - timedelta(hours=float(hours))).strftime("%d %b %H:%M")
        st.info(f"Will scan **{n_src} sources** for articles since **{since} (Nepal time)** - {label if presets[label] else f'last {hours} hours'}.")
        if st.button("▶️ Run scan now", type="primary"):
            bar = st.progress(0.0, text="Starting...")
            df, sdf, out = run_scan(cfg, lambda p, n: bar.progress(p, text=f"Checked: {n}"))
            bar.empty()
            save_config(cfg)
            st.session_state.result = (df, sdf, out.name, make_zip(out))

        if "result" in st.session_state:
            df, sdf, name, zbytes = st.session_state.result
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Articles found", len(df))
            m2.metric("🔴 Critical", int((df.priority == "Critical").sum()) if len(df) else 0)
            m3.metric("Sources OK", int((sdf.status == "OK").sum()))
            m4.metric("Sources failed", int((sdf.status == "FAILED").sum()))
            if len(df):
                pick = st.multiselect("Show priority", TIERS, default=TIERS)
                view = df[df.priority.isin(pick)].copy()
                view["priority"] = view["priority"].map(ICON)
                st.dataframe(view.drop(columns=["file", "fetched_at"]), hide_index=True,
                             column_config={"url": st.column_config.LinkColumn("Link", display_text="Open")})
                d1, d2 = st.columns(2)
                d1.download_button("⬇️ Download meta CSV", df.to_csv(index=False, encoding="utf-8-sig"), f"meta_{name}.csv", "text/csv")
                d2.download_button("⬇️ Download all (CSV + articles)", zbytes, f"heoc_news_{name}.zip", "application/zip")
            else:
                st.success("No new matching articles in this time window.")
            with st.expander("Source status (which sites worked)"):
                st.dataframe(sdf, hide_index=True)

    with t_hist:
        runs = sorted(RUNS_DIR.glob("*/meta.csv"), reverse=True) if RUNS_DIR.exists() else []
        if not runs:
            st.info("No saved scans yet. (On Streamlit Cloud, history resets when the app restarts.)")
        else:
            names = [p.parent.name for p in runs]
            sel = st.selectbox("Choose a scan (date_time)", names)
            try:
                h = pd.read_csv(RUNS_DIR / sel / "meta.csv")
                st.dataframe(h.drop(columns=["file", "fetched_at"], errors="ignore"), hide_index=True,
                             column_config={"url": st.column_config.LinkColumn("Link", display_text="Open")})
                st.download_button("⬇️ Download this scan", make_zip(RUNS_DIR / sel), f"heoc_news_{sel}.zip", "application/zip")
            except Exception as ex:
                st.warning(f"Could not read this scan: {short(ex)}")

    with t_help:
        st.markdown("""
**How to use**
1. Choose the **time window** on the left (e.g. last 2 hours).
2. Press **Run scan now** on the Dashboard.
3. Review the table: 🔴 Critical first. Click **Open** to read the news.
4. Download the CSV (opens in Excel) or the zip with the full article texts.

**Tips**
- *Sources* tab: add a news website, or switch one off. Use **Test all sources** if a site shows FAILED.
- *Keywords* tab: add English and Nepali words, one per line.
- Articles with no publish date are shown with a note - please verify them manually.
- This tool is a first filter. A researcher should confirm each item before reporting.
""")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        d, s, o = run_scan(load_config())
        print(f"Done: {len(d)} articles, {int((s.status == 'FAILED').sum())} failed sources. Saved in {o}")
    else:
        main_ui()
