#!/usr/bin/env python3
"""
Quant internship finder.

Sources (each isolated: one failing never blocks the others)
  nufintech   northwesternfintech/2027QuantInternships  (YAML data, quant firms only)
  simplify    SimplifyJobs/Summer2027-Internships       (structured JSON, "Quant" category + watchlist)
  greenhouse  public Greenhouse job-board API            (HRT, Anthropic, ... add any board token)
  janestreet  Jane Street's own job feed
  amazon      amazon.jobs search API
  pages       JS-rendered career pages read via Jina Reader (Citadel, Citadel Securities, Google)

Pipeline: fetch -> normalize -> merge/dedupe -> filter (undergrad, current cycle)
          -> enrich deadlines from posting text -> sort by deadline -> write data/ + docs/ -> notify new.

Usage: python finder.py [--sources a,b] [--no-notify] [--no-enrich] [-v]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import zipfile
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import requests
import yaml
from dateutil import parser as dateparser

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
TEMPLATE_PATH = os.path.join(ROOT, "templates", "dashboard.html")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
CACHE_PATH = os.path.join(DATA_DIR, "cache.json")
OUT_PATH = os.path.join(DATA_DIR, "internships.json")
DASH_PATH = os.path.join(DOCS_DIR, "index.html")

NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
VERBOSE = False

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; quant-intern-finder/1.0; personal job tracker)",
    "Accept": "application/json, text/plain, */*",
})


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def vlog(*a):
    if VERBOSE:
        log(*a)


# ----------------------------------------------------------------------------- text helpers
INTERN_RE = re.compile(r"\b(intern(ship)?s?|co-?op|summer analyst|student researcher)\b", re.I)
EVENT_RE = re.compile(r"\b(programs?|programmes?|events?|challenge|competition|invitational|datathon|hackathon|insight|discover|"
                      r"fellowships?|fellows?|scholarship|academy|bootcamp|immersion|summit|workshop|masterclass|open day|"
                      r"trading day|prize|ambassadors?|builder club|summer of code|colloquium|puzzle|olympiad)\b", re.I)
EVENT_TYPES = [  # first match wins
    ("datathon", r"datathon"), ("hackathon", r"hackathon|hack\b"), ("competition", r"competition|challenge|invitational|contest|prize|olympiad|puzzle"),
    ("fellowship", r"fellow"), ("scholarship", r"scholarship|grant"), ("ambassador", r"ambassador|builder club|campus program"),
    ("research", r"student researcher|research program|summer of code"),
    ("insight", r"insight|discover|inside |open day|immersion|masterclass|academy|bootcamp|focus|wise|see\b|fttp|first[- ]year|sophomore|women|explore"),
    ("program", r"program|programme|summit|workshop|colloquium|event"),
]
NAV_NOISE = re.compile(r"^(overview|internships?|full[- ]time|positions? for (students|professionals)|open opportunities|client login|"
                       r"early careers|campus sponsorships|career perspectives|students?|phds? and postdocs|experienced|careers?|apply|learn more|"
                       r"read more|home|about|news|contact|search|menu|back|next|previous|log ?in|sign ?up|jobs for grads|internships for students)$", re.I)
WHEN_RE = re.compile(r"\b((?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\.?\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,?\s+20\d{2}|"
                     r"(?:spring|summer|fall|autumn|winter)\s+20\d{2}|(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+20\d{2})\b", re.I)
ELIG_RE = re.compile(r"\b(first[- ]and[- ]second[- ]year|first[- ]year|second[- ]year|sophomore|freshman|junior|senior|undergraduate|graduating (?:in|between) [^.;]{4,60}|expected graduation [^.;]{4,80}|phd|graduate students?|high school)\b", re.I)


AUDIENCE_TAGS = [
    ("first/second-years", re.compile(r"first[- ]?(and|or|/)[- ]?second[- ]year|first[- ]year|second[- ]year|freshm[ae]n|sophomore|early undergrad|\b1st\b|\b2nd\b|graduating (?:in|between) 20(?:28|29)|dec(?:ember)? 2027 ?[-–] ?jun", re.I)),
    ("women+", re.compile(r"\bwomen|female|gender[- ]expansive|non-?binary|transgender", re.I)),
    ("barriers / first-gen", re.compile(r"barrier|underrepresented|under-?resourced|first[- ]generation|low[- ]income|pell|underprivileged", re.I)),
]


def audience(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    tags = [name for name, rx in AUDIENCE_TAGS if rx.search(blob)]
    if not tags and re.search(r"undergrad|university students|students", blob, re.I):
        tags = ["any undergrad"]
    return tags


def event_type(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    for name, rx in EVENT_TYPES:
        if re.search(rx, blob, re.I):
            return name
    return "program"
TERM_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\s*(?:of\s*)?(20\d{2})\b", re.I)

LEVEL_RULES = [
    ("phd", re.compile(r"\b(ph\.?\s?d|doctoral|doctorate|postdoc)\b", re.I)),
    ("mba", re.compile(r"\bmba\b", re.I)),
    ("masters", re.compile(r"\b(master'?s?|msc|m\.s\.|ms|graduate student|grad(uate)? intern)\b", re.I)),
    ("undergrad", re.compile(r"\b(undergrad(uate)?|bachelor'?s?|bs|ba|b\.s\.|b\.a\.)\b", re.I)),
]

ROLE_RULES = [
    ("QR", re.compile(r"quant(itative)?\s*research|researcher|research\s*(analyst|scientist|engineer)|\bresearch\b", re.I)),
    ("QT", re.compile(r"\btrader\b|\btrading\b", re.I)),
    ("QD", re.compile(r"quant(itative)?\s*(dev|develop|strateg|analyst|engineer)|strategy developer|\bqd\b", re.I)),
    ("ML", re.compile(r"machine learning|\bml\b|\bai\b|applied scien|data scien|deep learning|\bnlp\b", re.I)),
    ("HW", re.compile(r"fpga|hardware|asic|electrical|\bhw\b", re.I)),
    ("SWE", re.compile(r"software|engineer|developer|\bswe\b|\bsde\b|systems|infrastructure|security|technolog|programm", re.I)),
]
ROLE_TITLES = {  # for the NU FinTech list, which stores role codes instead of titles
    "QR": "Quantitative Researcher Intern", "QT": "Quantitative Trader Intern",
    "QD": "Quantitative Developer Intern", "SWE": "Software Engineer Intern",
    "HW": "Hardware Engineer Intern", "FPGA": "FPGA Engineer Intern",
    "ML": "Machine Learning Intern", "DevOps/SRE": "DevOps / SRE Intern",
    "QR Fellowship": "Quantitative Research Fellowship",
}

MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_FORMS = (
    rf"{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}(?:,?\s+\d{{4}})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
)
DEADLINE_RE = re.compile(
    r"(?:application deadline|deadline to apply|apply(?:ing)?\s+(?:by|before|no later than)|"
    r"applications?\s+(?:are\s+|will\s+be\s+)?(?:close|closes|closing|due|must be (?:submitted|received))|"
    r"closing date|close(?:s)? on|deadline(?:\s+is|:)?|priority deadline|submit(?:ted)? by|"
    r"(?:will|must) be (?:considered|reviewed) (?:until|through|by))"
    rf"[^.\n]{{0,60}}?(?P<date>{DATE_FORMS})",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = TAG_RE.sub(" ", s)
    return WS_RE.sub(" ", s).strip()


def parse_date_str(raw: str) -> dt.date | None:
    """Parse a human date; if the year is missing, take the next occurrence on/after today."""
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip())
    has_year = bool(re.search(r"\b(19|20)\d{2}\b", raw)) or bool(re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", raw))
    try:
        d = dateparser.parse(raw, default=dt.datetime(TODAY.year, 1, 1)).date()
    except (ValueError, OverflowError):
        return None
    if not has_year and d < TODAY - dt.timedelta(days=30):
        d = d.replace(year=d.year + 1)
    return d


def extract_deadline(text: str) -> str | None:
    if not text:
        return None
    for m in DEADLINE_RE.finditer(text):
        d = parse_date_str(m.group("date"))
        if d and TODAY - dt.timedelta(days=400) < d < TODAY + dt.timedelta(days=730):
            return d.isoformat()
    return None


def extract_term(*texts: str) -> str | None:
    for t in texts:
        if not t:
            continue
        m = TERM_RE.search(t)
        if m:
            season = m.group(1).title().replace("Autumn", "Fall")
            return f"{season} {m.group(2)}"
    return None


def infer_level(title: str, degrees: list[str] | None = None) -> str:
    degrees = [d.lower() for d in (degrees or [])]
    if any("bachelor" in d for d in degrees):
        return "undergrad"
    if degrees:
        if any("phd" in d or "doctor" in d for d in degrees):
            return "phd"
        if any("mba" in d for d in degrees):
            return "mba"
        if any("master" in d for d in degrees):
            return "masters"
    found = [level for level, rx in LEVEL_RULES if rx.search(title)]
    if "undergrad" in found:  # "BS/MS", "Bachelor's or Master's": undergrads are eligible
        return "undergrad"
    return found[0] if found else "unknown"


def infer_role(title: str) -> str:
    for code, rx in ROLE_RULES:
        if rx.search(title):
            return code
    return "Other"


GH_ID_RES = [re.compile(r"[?&]gh_jid=(\d+)"), re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?[^/?#]+/jobs/(\d+)"),
             re.compile(r"greenhouse\.io/embed/job_app\?(?:[^#]*&)?token=(\d+)"),
             re.compile(r"/(\d{9,})/?(?:[?#]|$)")]


def dedupe_key(u: str) -> str:
    """Same posting reached through different URLs (company site vs Greenhouse) -> same key."""
    for rx in GH_ID_RES:
        m = rx.search(u)
        if m:
            return f"gh:{m.group(1)}"
    return norm_url(u)


def norm_url(u: str | None) -> str:
    if not u:
        return ""
    p = urlparse(u.strip())
    drop = {"gh_src", "source", "src", "ref", "mobile", "needsRedirect", "lang", "trk", "trackingTag", "utm_source",
            "utm_medium", "utm_campaign", "utm_content", "utm_term", "hl", "gclid"}
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in drop]
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA MA MD ME MI MN MO MS MT NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY DC PR".split())
US_STATE_NAMES = (r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|puerto rico")
US_CITIES = (r"nyc|new york|new york city|manhattan|brooklyn|chicago|austin|boston|san francisco|south sf|\bsf\b|\bla\b|los angeles|miami|greenwich|stamford|westport|philadelphia|bala cynwyd|ardmore|houston|dallas|seattle|bellevue|redmond|jersey city|hoboken|princeton|denver|atlanta|charlotte|minneapolis|milwaukee|jupiter|palo alto|menlo park|mountain view|san jose|sunnyvale|santa clara|cupertino|redwood city|oakland|berkeley|irvine|newport beach|san diego|pittsburgh|cleveland|columbus|detroit|allen park|phoenix|scottsdale|salt lake|boca raton|orlando|tampa|nashville|newark|bethesda|reston|mclean|arlington|richmond, va|plano|portland|baltimore|kansas city|st\. louis|cincinnati|indianapolis|raleigh|durham|louisville|omaha|hartford|providence|wilmington|setauket|samford|remote in usa?|remote in the us|\bremote\b|\busa\b|\bu\.s\.|united states")
NON_US = (r"\buk\b|united kingdom|england|scotland|london|bristol|edinburgh|manchester|oxford|dublin|ireland|amsterdam|netherlands|paris|france|germany|frankfurt|berlin|munich|freiburg|switzerland|zurich|zürich|geneva, |hong kong|singapore|sydney|australia|melbourne|tokyo|japan|shanghai|beijing|shenzhen|china|india|mumbai|bangalore|bengaluru|gurgaon|gurugram|hyderabad|pune|chennai|delhi|canada|toronto|montreal|montréal|vancouver|ottawa|calgary|waterloo|kitchener|ontario|quebec|israel|tel aviv|dubai|emirates|\buae\b|madrid|barcelona|spain|milan|italy|stockholm|sweden|copenhagen|denmark|warsaw|poland|taipei|taiwan|seoul|korea|brazil|são paulo|sao paulo|mexico|luxembourg|brussels|belgium|vienna|austria|prague|lisbon|portugal|athens|greece|cape town|south africa|remote in canada|remote in uk|remote in europe|\beu\b|europe")
US_CITY_RE = re.compile(US_CITIES + "|" + US_STATE_NAMES, re.I)
NON_US_RE = re.compile(NON_US, re.I)


def classify_location(loc: str) -> str:
    """'US', 'non-US', or 'unknown' for one location string."""
    if not loc:
        return "unknown"
    if NON_US_RE.search(loc):
        return "non-US"
    m = re.search(r"(?:^|,)\s*([A-Za-z]{2})\s*$", loc.strip())
    if m and m.group(1).upper() in US_STATES:
        return "US"
    if US_CITY_RE.search(loc):
        return "US"
    return "unknown"


def us_status(locations: list[str]) -> str:
    kinds = {classify_location(l) for l in locations}
    if "US" in kinds:
        return "US"
    if "non-US" in kinds:
        return "non-US"
    return "unknown"


def split_locs(s: str | None) -> list[str]:
    """'Chicago, NYC' -> two places; 'Boston, MA' -> one; 'New York, NY; Boston, MA' -> two."""
    if not s:
        return []
    parts = [x.strip() for x in re.split(r"[;/|]| and ", s) if x.strip()]
    out = []
    for part in parts:
        toks = [t.strip() for t in part.split(",") if t.strip()]
        cur = []
        for t in toks:
            if cur and (t.upper() in US_STATES or re.fullmatch(US_STATE_NAMES, t, re.I) or t.lower() in ("canada", "uk", "usa")):
                cur[-1] = f"{cur[-1]}, {t}"
            else:
                cur.append(t)
        out.extend(cur)
    return out


# ----------------------------------------------------------------------------- config-bound helpers
CFG: dict = {}
ALIAS_MAP: dict[str, str] = {}
WATCH: list[str] = []
WATCH_GROUP: dict[str, str] = {}


def load_config():
    global CFG, ALIAS_MAP, WATCH, WATCH_GROUP
    with open(CONFIG_PATH, encoding="utf-8") as f:
        CFG = json.load(f)
    ALIAS_MAP = {}
    for canon, aliases in CFG.get("company_aliases", {}).items():
        ALIAS_MAP[canon.lower()] = canon
        for a in aliases:
            ALIAS_MAP[a.lower()] = canon
    groups = CFG.get("watchlist_groups") or {"quant": CFG.get("watchlist", [])}
    WATCH_GROUP = {name: g for g, names in groups.items() for name in names}
    WATCH = list(WATCH_GROUP)


def canon_company(name: str | None) -> str:
    n = WS_RE.sub(" ", (name or "")).strip()
    return ALIAS_MAP.get(n.lower(), n)


def watch_name(company: str) -> str | None:
    """Longest watchlist entry that the company name starts with (so 'Citadel Securities' beats 'Citadel')."""
    c = company.lower()
    hits = [w for w in WATCH if c == w.lower() or c.startswith(w.lower() + " ")]
    return max(hits, key=len) if hits else None


def listing(**kw) -> dict:
    company = canon_company(kw.get("company"))
    title = WS_RE.sub(" ", kw.get("title") or "").strip()
    url = (kw.get("url") or "").strip()
    key = dedupe_key(url) if url else f"{company.lower()}|{title.lower()}"
    if kw.get("kind") == "event":  # several programs often share one hub URL
        key = f"event|{company.lower()}|{key}|{re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()}"
    text = kw.get("text") or ""
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:12],
        "company": company,
        "title": title,
        "locations": [l for l in (kw.get("locations") or []) if l],
        "url": url,
        "sources": [kw.get("source") or "unknown"],
        "role": kw.get("role_type") or infer_role(title),
        "term": kw.get("term") or extract_term(title, text[:2000]),
        "degrees": kw.get("degrees") or [],
        "level": kw.get("level") or infer_level(title, kw.get("degrees")),
        "date_posted": kw.get("date_posted"),
        "deadline": kw.get("deadline") or extract_deadline(text),
        "deadline_source": "posting" if (kw.get("deadline") or extract_deadline(text)) else None,
        "kind": kw.get("kind") or "internship",
        "status": kw.get("status") or "open",  # open | upcoming (opens soon) | closed (last cycle over; expected to return)
        "event_type": kw.get("event_type"),
        "when": kw.get("when"),
        "eligibility": kw.get("eligibility"),
        "audience": audience(title, kw.get("eligibility") or "", kw.get("note") or "", text[:600]) if kw.get("kind") == "event" else [],
        "seeded": bool(kw.get("seeded")),
        "watch": watch_name(company),
        "watch_group": WATCH_GROUP.get(watch_name(company) or "", None),
        "us": us_status([l for l in (kw.get("locations") or []) if l]),
        "sponsorship": kw.get("sponsorship"),
        "pay": kw.get("pay"),
        "note": kw.get("note"),
        "_text": text,  # dropped before writing
    }


# ----------------------------------------------------------------------------- sources
def src_nufintech(cfg):
    r = SESSION.get(cfg["zip_url"], timeout=90)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    out, firms, notes = [], set(), {}
    for name in z.namelist():
        if "/data/" not in name or not name.endswith((".yaml", ".yml")):
            continue
        d = yaml.safe_load(z.read(name)) or {}
        company = canon_company(d.get("name"))
        if not company:
            continue
        firms.add(company)
        notes[company] = {"website": d.get("website"), "locations": d.get("locations"), "notes": d.get("notes")}
        for role in d.get("roles") or []:
            rt = (role.get("role_type") or "").strip()
            for link in role.get("links") or []:
                label = (link.get("label") or "").strip()
                title = ROLE_TITLES.get(rt, f"{rt} Intern") + (f" ({label})" if label else "")
                out.append(listing(company=company, title=title, locations=split_locs(d.get("locations")),
                                   url=link.get("url"), source="nufintech", term=cfg.get("term"),
                                   role_type=rt if rt in ROLE_TITLES else None,
                                   level="phd" if re.search(r"ph\.?d", label, re.I) else None))
    return out, {"firms": firms, "notes": notes}


def src_simplify(cfg, ctx, name="simplify"):
    r = SESSION.get(cfg["url"], timeout=90)
    r.raise_for_status()
    rows = [x for x in r.json() if x.get("active") and x.get("is_visible", True)]
    quant_cat = cfg.get("quant_category", "Quant")
    # a "quant firm" = in the Northwestern list, or has any listing in Simplify's Quant category,
    # or is polled via Greenhouse in config, or is named in config.quant_firms
    firms = {f.lower() for f in ctx.get("firms", set())}
    firms |= {canon_company(x.get("company_name")).lower() for x in rows if x.get("category") == quant_cat}
    firms |= {canon_company(c).lower() for c in CFG.get("sources", {}).get("greenhouse", {}).get("boards", {})}
    firms |= {canon_company(c).lower() for c in CFG.get("quant_firms", [])}
    ctx["quant_firms"] = firms | set(ctx.get("quant_firms", set()))
    out = []
    for x in rows:
        company = canon_company(x.get("company_name"))
        cat = x.get("category") or ""
        if cat != quant_cat and not watch_name(company) and company.lower() not in firms:
            continue
        dp = x.get("date_posted")
        terms = [t for t in (x.get("terms") or []) if t and t != "N/A"]
        out.append(listing(company=company, title=x.get("title"), locations=x.get("locations") or [],
                           url=x.get("url"), source=name, term=terms[0] if terms else None,
                           degrees=x.get("degrees") or [], sponsorship=x.get("sponsorship"),
                           date_posted=dt.date.fromtimestamp(dp).isoformat() if dp else None))
    return out


def src_greenhouse(cfg, ctx):
    out = []
    for company, board in cfg["boards"].items():
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        try:
            r = SESSION.get(url, timeout=60)
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
        except Exception as e:
            log(f"  greenhouse: {company} ({board}) -> {e}")
            ctx.setdefault("partial_errors", []).append(f"{company}/{board}: {type(e).__name__} {str(e)[:80]}")
            continue
        for j in jobs:
            title = j.get("title") or ""
            is_intern = bool(INTERN_RE.search(title))
            is_event = not is_intern and bool(re.search(r"\b(fellow|fellowship|program|challenge|invitational|datathon|hackathon|scholar|academy|bootcamp)\b", title, re.I))
            if not (is_intern or is_event):
                continue
            posted = (j.get("first_published") or j.get("updated_at") or "")[:10] or None
            text = strip_html(j.get("content", ""))
            out.append(listing(company=company, title=title, locations=[(j.get("location") or {}).get("name", "")],
                               url=j.get("absolute_url"), source=f"greenhouse:{board}", date_posted=posted, text=text,
                               kind="event" if is_event else "internship",
                               event_type=event_type(title) if is_event else None,
                               eligibility=(ELIG_RE.search(text) or [None])[0] if is_event else None))
    return out


def src_janestreet(cfg, ctx):
    r = SESSION.get(cfg["url"], timeout=60)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
    out = []
    for j in items:
        if not isinstance(j, dict):
            continue
        title = j.get("title") or j.get("name") or ""
        strs = " ".join(str(v) for k, v in j.items() if isinstance(v, str) and k not in ("overview", "description"))
        if not (INTERN_RE.search(title) or re.search(r"intern", strs, re.I)):
            continue
        url = j.get("url") or j.get("absolute_url") or ""
        if not url and j.get("id"):
            url = cfg["position_url"].format(id=j["id"])
        if url.startswith("/"):
            url = "https://www.janestreet.com" + url
        loc = j.get("city") or j.get("location") or j.get("locations") or ""
        locs = loc if isinstance(loc, list) else split_locs(str(loc))
        text = strip_html(str(j.get("overview") or j.get("description") or ""))
        out.append(listing(company="Jane Street", title=title, locations=locs, url=url, source="janestreet",
                           term=extract_term(str(j.get("season") or ""), str(j.get("duration") or ""), title, strs),
                           text=text))
    return out


def src_amazon(cfg, ctx):
    out, seen = [], set()
    countries = set(c.upper() for c in cfg.get("countries", []))
    for q in cfg["queries"]:
        params = {"base_query": q, "offset": 0, "result_limit": 100, "sort": "recent", "radius": "24km"}
        try:
            r = SESSION.get(cfg["url"], params=params, timeout=60)
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
        except Exception as e:
            log(f"  amazon: '{q}' -> {e}")
            ctx.setdefault("partial_errors", []).append(f"amazon '{q}': {type(e).__name__} {str(e)[:80]}")
            continue
        for j in jobs:
            title = j.get("title") or ""
            path = j.get("job_path") or ""
            if not INTERN_RE.search(title) or not path or path in seen:
                continue
            if countries and (j.get("country_code") or "").upper() not in countries:
                continue
            seen.add(path)
            posted = None
            if j.get("posted_date"):
                d = parse_date_str(str(j["posted_date"]))
                posted = d.isoformat() if d else None
            loc = j.get("normalized_location") or ", ".join(x for x in [j.get("city"), j.get("state")] if x)
            text = strip_html(" ".join(str(j.get(k) or "") for k in ("description", "basic_qualifications", "preferred_qualifications")))
            out.append(listing(company="Amazon", title=title, locations=[loc] if loc else [],
                               url="https://www.amazon.jobs" + path, source="amazon", date_posted=posted, text=text))
    if not out:  # JSON API gave nothing: read the HTML search page through Jina Reader instead
        log("  amazon: API returned no jobs, reading search pages via Jina")
        pat = re.compile(r"amazon\.jobs/en/jobs/\d+")
        for q in cfg["queries"]:
            try:
                md = jina_read(f"https://www.amazon.jobs/en/search?base_query={quote(q)}&country=USA")
            except Exception as e:
                ctx.setdefault("partial_errors", []).append(f"amazon page '{q}': {type(e).__name__} {str(e)[:80]}")
                continue
            finally:
                time.sleep(3.5)
            for m in MD_LINK_RE.finditer(md):
                text, href = m.group(1), m.group(2)
                key = dedupe_key(href)
                title = WS_RE.sub(" ", re.sub(r"[*_`#!\[\]]", " ", text)).strip()
                if not pat.search(href) or key in seen or not INTERN_RE.search(title):
                    continue
                seen.add(key)
                out.append(listing(company="Amazon", title=title, url=href, source="amazon"))
    return out


def src_lever(cfg, ctx):
    out = []
    for company, slug in cfg["boards"].items():
        try:
            r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=60)
            r.raise_for_status()
            jobs = r.json()
        except Exception as e:
            ctx.setdefault("partial_errors", []).append(f"{company}/{slug}: {type(e).__name__} {str(e)[:80]}")
            continue
        for j in jobs:
            title = j.get("text") or ""
            if not INTERN_RE.search(title):
                continue
            cat = j.get("categories") or {}
            locs = [x for x in [cat.get("location")] + (j.get("allLocations") or []) if x]
            posted = dt.datetime.fromtimestamp(j["createdAt"] / 1000, dt.timezone.utc).date().isoformat() if j.get("createdAt") else None
            out.append(listing(company=company, title=title, locations=list(dict.fromkeys(locs)), url=j.get("hostedUrl"),
                               source=f"lever:{slug}", date_posted=posted, text=strip_html(j.get("descriptionPlain") or j.get("description") or "")))
    return out


def src_ashby(cfg, ctx):
    out = []
    for company, slug in cfg["boards"].items():
        try:
            r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false", timeout=60)
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
        except Exception as e:
            ctx.setdefault("partial_errors", []).append(f"{company}/{slug}: {type(e).__name__} {str(e)[:80]}")
            continue
        for j in jobs:
            title = j.get("title") or ""
            if not INTERN_RE.search(title) or j.get("isListed") is False:
                continue
            locs = [j.get("location")] + [x.get("location") for x in (j.get("secondaryLocations") or []) if isinstance(x, dict)]
            out.append(listing(company=company, title=title, locations=[x for x in locs if x], url=j.get("jobUrl"),
                               source=f"ashby:{slug}", date_posted=(j.get("publishedAt") or "")[:10] or None,
                               text=strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or "")))
    return out


def src_workday(cfg, ctx):
    """Workday's public career-site search (POST …/wday/cxs/{tenant}/{site}/jobs)."""
    out = []
    for company, wd in cfg["sites"].items():
        base = f"https://{wd['host']}/wday/cxs/{wd['tenant']}/{wd['site']}"
        for q in wd.get("queries", ["intern"]):
            offset, seen = 0, set()
            while offset < 200:
                try:
                    r = SESSION.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q},
                                     headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=60)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    ctx.setdefault("partial_errors", []).append(f"{company} '{q}': {type(e).__name__} {str(e)[:80]}")
                    break
                posts = data.get("jobPostings") or []
                for j in posts:
                    title = j.get("title") or ""
                    path = j.get("externalPath") or ""
                    if not INTERN_RE.search(title) or not path or path in seen:
                        continue
                    seen.add(path)
                    out.append(listing(company=company, title=title, locations=[j.get("locationsText") or ""],
                                       url=f"https://{wd['host']}/en-US/{wd['site']}{path}", source=f"workday:{wd['tenant']}",
                                       text=title + " " + (j.get("bulletFields") and " ".join(map(str, j["bulletFields"])) or "")))
                if len(posts) < 20:
                    break
                offset += 20
    return out


def src_microsoft(cfg, ctx):
    out, seen = [], set()
    for q in cfg.get("queries", ["intern"]):
        for page in range(1, 6):
            try:
                r = SESSION.get("https://gcsservices.careers.microsoft.com/search/api/v1/search",
                                params={"q": q, "l": "en_us", "pg": page, "pgSz": 20, "o": "Recent", "flt": "true"}, timeout=60)
                r.raise_for_status()
                jobs = (((r.json().get("operationResult") or {}).get("result") or {}).get("jobs")) or []
            except Exception as e:
                ctx.setdefault("partial_errors", []).append(f"microsoft '{q}' p{page}: {type(e).__name__} {str(e)[:80]}")
                break
            for j in jobs:
                title = j.get("title") or ""
                jid = str(j.get("jobId") or "")
                if not jid or jid in seen or not INTERN_RE.search(title):
                    continue
                seen.add(jid)
                props = j.get("properties") or {}
                locs = props.get("locations") or [props.get("primaryLocation")] if props else []
                out.append(listing(company="Microsoft", title=title, locations=[x for x in locs if x],
                                   url=f"https://jobs.careers.microsoft.com/global/en/job/{jid}", source="microsoft",
                                   date_posted=(j.get("postingDate") or "")[:10] or None,
                                   text=strip_html(props.get("description") or "")))
            if len(jobs) < 20:
                break
    return out


SA_ROW_RE = re.compile(r"^\|\s*(?P<company>.*?)\s*\|\s*(?P<title>.*?)\s*\|\s*(?P<loc>.*?)\s*\|\s*(?P<pay>.*?)\s*\|\s*(?P<post>.*?)\s*\|\s*(?P<age>.*?)\s*\|\s*$")


def src_speedyapply(cfg, ctx):
    """speedyapply's daily-updated markdown lists (SWE + AI). Sections: FAANG+, Quant, Other."""
    out = []
    firms = ctx.get("quant_firms", set())
    for name, url in cfg["lists"].items():
        try:
            r = SESSION.get(url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            ctx.setdefault("partial_errors", []).append(f"{name}: {type(e).__name__} {str(e)[:80]}")
            continue
        section, in_usa = None, False
        for line in r.text.splitlines():
            if line.startswith("## "):
                in_usa = "USA" in line and "Intern" in line
                continue
            if line.startswith("### "):
                section = line[4:].strip().lower()
                continue
            if not in_usa or not line.startswith("| <a"):
                continue
            m = SA_ROW_RE.match(line)
            if not m:
                continue
            company = canon_company(strip_html(m.group("company")))
            title = strip_html(m.group("title"))
            href = re.search(r'href="([^"]+)"', m.group("post"))
            if not company or not title or not href:
                continue
            if not (section == "quant" or watch_name(company) or company.lower() in firms):
                continue
            loc = strip_html(m.group("loc"))
            extra = re.search(r"\+(\d+)$", loc)
            loc = re.sub(r"\s*\+\d+$", "", loc)
            age = re.match(r"(\d+)\s*(h|d|w|mo)", strip_html(m.group("age")))
            posted = None
            if age:
                n, unit = int(age.group(1)), age.group(2)
                days = {"h": 0, "d": n, "w": n * 7, "mo": n * 30}[unit]
                posted = (TODAY - dt.timedelta(days=days)).isoformat()
            pay = strip_html(m.group("pay")) or None
            out.append(listing(company=company, title=title, locations=[loc] if loc else [], url=href.group(1),
                               source=f"speedyapply:{name}", date_posted=posted, pay=pay,
                               note=f"{extra.group(1)} more location(s) on the posting" if extra else None))
    return out


MD_LINK_RE = re.compile(r"\[([^\]]{3,300})\]\((https?://[^\s)]+)\)")


def jina_read(url: str, timeout: int = 120) -> str:
    """Jina Reader (the same backend Agent Reach routes web reads through): any URL -> markdown, JS rendered."""
    r = SESSION.get("https://r.jina.ai/" + url, headers={"Accept": "text/plain", "X-Return-Format": "markdown"}, timeout=timeout)
    r.raise_for_status()
    return r.text


def src_pages(cfg, ctx):
    out = []
    for page in cfg["pages"]:
        log(f"  pages: reading {page['company']} …")
        try:
            md = jina_read(page["url"])
        except Exception as e:  # keep going with the remaining pages
            log(f"  pages: {page['company']} {page['url']} -> {e}")
            ctx.setdefault("partial_errors", []).append(f"{page['company']}: {type(e).__name__} {str(e)[:80]}")
            continue
        finally:
            time.sleep(3.5)  # r.jina.ai allows ~20 requests/min without a key
        pat = re.compile(page["link_pattern"])
        seen = set()
        for m in MD_LINK_RE.finditer(md):
            text, href = m.group(1), m.group(2)
            if not pat.search(href):
                continue
            key = dedupe_key(href)
            title = WS_RE.sub(" ", re.sub(r"[*_`#!\[\]]", " ", text)).strip()
            title = re.split(r"\s{2,}|\s[|•·]\s", title)[0].strip()
            if key in seen or not INTERN_RE.search(title):
                continue
            seen.add(key)
            out.append(listing(company=page["company"], title=title,
                               locations=[page["location"]] if page.get("location") else [],
                               url=href, source=f"page:{page['company']}", term=page.get("term")))
    return out


def _md_sections(md: str):
    """Yield (link_text, href, following_text) for every markdown link, with the prose that follows it."""
    ms = list(MD_LINK_RE.finditer(md))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md)
        yield m.group(1), m.group(2), md[m.end():end]


def clean_title(text: str) -> str:
    t = WS_RE.sub(" ", re.sub(r"[*_`#!\[\]]|!\[[^\]]*\]\([^)]*\)", " ", text)).strip(" -–·|")
    return re.split(r"\s{2,}|\s[|•·]\s", t)[0].strip()


def src_events(cfg, ctx):
    out = []
    for seed in cfg.get("seeded", []):  # known programs; shown even when the scrape fails
        out.append(listing(company=seed["company"], title=seed["title"], url=seed.get("url"), source="events:seed",
                           kind="event", event_type=seed.get("type") or event_type(seed["title"], seed.get("note", "")),
                           when=seed.get("when"), eligibility=seed.get("eligibility"), note=seed.get("note"),
                           locations=seed.get("locations") or [], level=seed.get("level"), seeded=True,
                           deadline=seed.get("deadline")))
    for page in cfg.get("pages", []):
        log(f"  events: reading {page['company']} programs page …")
        try:
            md = jina_read(page["url"])
        except Exception as e:
            ctx.setdefault("partial_errors", []).append(f"{page['company']} events: {type(e).__name__} {str(e)[:80]}")
            continue
        finally:
            time.sleep(3.5)
        if page.get("self"):  # the page itself is one opportunity (e.g. "Apply to YC"); pull its dates from the text
            body = WS_RE.sub(" ", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md))[:8000]
            when = WHEN_RE.search(body)
            elig = ELIG_RE.search(body)
            out.append(listing(company=page["company"], title=page["title"], url=page["url"], source=f"events:{page['company']}",
                               kind="event", event_type=page.get("type") or event_type(page["title"], body[:500]), text=body,
                               when=when.group(0) if when else None, eligibility=elig.group(0) if elig else None,
                               note=page.get("note")))
            continue
        pat = re.compile(page["link_pattern"])
        seen = set()
        for text, href, after in _md_sections(md):
            if not pat.search(href):
                continue
            title = clean_title(text)
            if not (3 <= len(title) <= 120) or NAV_NOISE.match(title):
                continue
            if page.get("require_keyword", True) and not EVENT_RE.search(title + " " + after[:200]):
                continue
            key = dedupe_key(href)
            if key in seen:
                continue
            seen.add(key)
            snippet = WS_RE.sub(" ", re.sub(r"[#*_>\[\]]|\(https?://[^)]*\)", " ", after)).strip()[:400]
            when = WHEN_RE.search(snippet)
            elig = ELIG_RE.search(snippet)
            out.append(listing(company=page["company"], title=title, url=href, source=f"events:{page['company']}",
                               kind="event", event_type=event_type(title, snippet), text=snippet,
                               when=when.group(0) if when else None, eligibility=elig.group(0) if elig else None,
                               note=snippet[:280] if snippet else None))
    return out


UC_DEADLINE_RE = re.compile(rf"deadline:?\s*(?P<d>{DATE_FORMS})", re.I)
UC_CLOSED_RE = re.compile(r"\bclosed\b|\bexpired\b|🔒", re.I)
UC_SOON_RE = re.compile(r"opens? soon|coming soon|not yet open|⏳", re.I)
UC_COLS = [("company", r"company"), ("title", r"\b(role|program|name)\b"), ("loc", r"location"),
           ("apply", r"application|apply"), ("posted", r"date posted"), ("status", r"status"),
           ("desc", r"description"), ("approx", r"deadline"), ("year", r"\byear\b"),
           ("note", r"\bnote\b"), ("ptype", r"\btype\b")]


def _uc_tables(md: str):
    """Yield (section, colmap, cells) for every data row of every markdown table, tracked by ## section."""
    section, cols = "", None
    for line in md.splitlines():
        if line.startswith("## "):
            section = WS_RE.sub(" ", re.sub(r"[#*_`]|[^\x00-\x7f]", " ", line[3:])).strip()
            cols = None
            continue
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|") and len(s) > 2):
            cols = None
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
            continue
        if cols is None:  # first pipe row of a table = header
            cols = {}
            for i, c in enumerate(cells):
                for key, pat in UC_COLS:
                    if key not in cols and re.search(pat, c, re.I):
                        cols[key] = i
                        break
            continue
        yield section, cols, cells


UC_GENERIC = {"student", "students", "first", "global", "management", "new", "the", "a", "an", "national",
              "university", "campus", "women", "summer", "career", "tech", "engineering", "software", "ai",
              "spring", "fall", "winter", "autumn"}


def _uc_company(title: str, url: str, known: dict[str, str]) -> str:
    words = [w.strip(":,.–—") for w in re.sub(r"^the\s+", "", title, flags=re.I).split()]
    for n in (4, 3, 2, 1):  # longest known-company prefix of the program name
        c = canon_company(" ".join(words[:n]))
        if c.lower() in known:
            return known[c.lower()]
    for name in sorted(known.values(), key=len, reverse=True):  # else a known company anywhere in the name
        if len(name) >= 3 and re.search(rf"\b{re.escape(name)}\b", title, re.I):
            return name
    host = re.sub(r"^https?://|/.*$", "", url).split(".")  # else a known company in the link's domain
    for part in host[:-1]:
        c = canon_company(part)
        if c.lower() in known:
            return known[c.lower()]
    if (len(words) >= 2 and words[1][:1].isupper()
            and not re.match(r"(fellowship|program|internship|externship|scholarship|challenge|academy|conference|"
                             r"membership|initiative|ambassadors?|leaders?|experts?|prep|tech|api|early|accelerate|"
                             r"cup|summit|insight|network(ing)?|collective|creators?|builders?)s?$", words[1], re.I)):
        return canon_company(" ".join(words[:2]))  # "Harvey Mudd …", "First Play …", "Lime Connect …"
    return canon_company(words[0]) if words and words[0].lower() not in UC_GENERIC else ""


def src_underclassmen(cfg, ctx):
    """Community-maintained GitHub lists of freshman/sophomore programs & internships (markdown tables)."""
    pools = (list(WATCH_GROUP), list(CFG.get("company_aliases", {})), CFG.get("quant_firms", []),
             list(ctx.get("firms", set())), cfg.get("companies", []))
    known = {canon_company(c).lower(): canon_company(c) for pool in pools for c in pool}
    qf = set(ctx.get("quant_firms") or set())
    if not qf:
        qf = {canon_company(c).lower() for c in CFG.get("quant_firms", [])}
        qf |= {canon_company(c).lower() for c in CFG.get("sources", {}).get("greenhouse", {}).get("boards", {})}
        qf |= {canon_company(c).lower() for c in ctx.get("firms", set())}
    out = []
    for repo, rc in cfg.get("repos", {}).items():
      sections = {k.lower(): v for k, v in rc.get("sections", {}).items()}
      for src_url, forced in ((rc["url"], None), (rc.get("archive"), "closed")):
        if not src_url:
            continue
        try:
            r = SESSION.get(src_url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            ctx.setdefault("partial_errors", []).append(f"{repo}: {type(e).__name__} {str(e)[:80]}")
            continue
        for section, cols, cells in _uc_tables(r.text):
            kind = next((v for k, v in sections.items() if section.lower().startswith(k)), None)
            if not kind:
                continue
            get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""
            st = get("status")
            status = forced or ("closed" if UC_CLOSED_RE.search(st) else "upcoming" if UC_SOON_RE.search(st) else "open")
            raw = get("title")
            m = MD_LINK_RE.search(raw)
            href = re.search(r'href="([^"]+)"', get("apply"))
            url = href.group(1) if href else (m.group(2) if m else "")
            blob = re.sub(r":[a-z_0-9]+:", " ", strip_html(m.group(1) if m else raw))  # drop :lock:-style shortcodes
            if re.search(r"🔒|:lock:|:no_entry|❌", raw):
                status = "closed"
            title = clean_title(re.split(r"\s*[—–-]{1,2}\s*deadline", blob, flags=re.I)[0])
            pm = re.match(r"^(.*?)\s*\(([^()]{5,120})\)$", title)
            paren = None
            if pm and not (len(pm.group(2)) <= 14 and pm.group(2).upper() == pm.group(2)):  # keep short acronyms: (JSIP)
                title, paren = pm.group(1).strip(), pm.group(2)
            if not (3 <= len(title) <= 140) or (kind != "event" and not url):
                continue
            dm = UC_DEADLINE_RE.search(strip_html(raw))
            deadline, when = dm.group("d") if dm else None, None
            approx = strip_html(get("approx"))
            if approx and not deadline:
                am = re.search(DATE_FORMS, approx, re.I)
                if am and re.search(r"\d", am.group(0)):
                    deadline = am.group(0)
                elif not re.search(r"no fixed|rolling|\?", approx, re.I):
                    when = f"usually {approx.rstrip('.').strip()}"
            desc = strip_html(get("desc"))
            year = strip_html(get("year")).strip(" ?")
            note = "; ".join(x for x in (paren, desc[:280] or None, strip_html(get("note"))[:200] or None) if x) or None
            elig = ELIG_RE.search(" ".join((blob, paren or "", desc[:600], year)))
            locs = [l for l in split_locs(strip_html(get("loc"))) if not re.search(r"check site|see site|various", l, re.I)]
            company = canon_company(strip_html(get("company"))) or _uc_company(title, url, known)
            if not company:
                continue
            if cfg.get("keep", "watchlist") != "all":
                w = watch_name(company)
                if not (w or company.lower() in qf):
                    continue
                if WATCH_GROUP.get(w) == "tech":  # same discipline as tech-group internships: relevant roles only
                    kws = CFG.get("tech_title_keywords", [])
                    if kws and not any(re.search(k, f"{title} {desc[:300]}", re.I) for k in kws):
                        continue
                    if any(re.search(p, title, re.I) for p in CFG.get("tech_exclude_patterns", []))                             or re.search(r"influencer|brand ambassador|\bcreators?\b", title, re.I):
                        continue
            out.append(listing(company=company, title=title, locations=locs, url=url,
                               source=f"underclassmen:{repo}", kind=kind, text=f"{blob} {desc[:600]}",
                               event_type=event_type(strip_html(get("ptype")), title, desc[:300]) if kind == "event" else None,
                               eligibility=year or (elig.group(0) if elig else None), when=when,
                               deadline=deadline, note=note, status=status,
                               date_posted=(lambda d: d.isoformat() if d else None)(parse_date_str(strip_html(get("posted"))))))
    return out


# ----------------------------------------------------------------------------- pipeline
def merge_two(a: dict, b: dict) -> dict:
    m = dict(a)
    m["sources"] = sorted(set(a["sources"]) | set(b["sources"]))
    if a.get("status") != b.get("status"):
        m["status"] = "open" if "open" in (a.get("status"), b.get("status")) else "upcoming"
    m["locations"] = list(dict.fromkeys(a["locations"] + b["locations"]))
    m["us"] = us_status(m["locations"])
    if "nufintech" in a["sources"] and "nufintech" not in b["sources"] and INTERN_RE.search(b["title"]):
        m["title"] = b["title"]  # real posting titles beat the role-code placeholder titles
    for k in ("term", "date_posted", "deadline", "deadline_source", "sponsorship", "note", "url", "pay"):
        if not m.get(k) and b.get(k):
            m[k] = b[k]
    if not m["degrees"] and b["degrees"]:
        m["degrees"] = b["degrees"]
    if b["level"] == "undergrad" or (m["level"] == "unknown" and b["level"] != "unknown"):
        m["level"] = b["level"]  # any source saying undergrads are eligible wins
    if m["role"] == "Other" and b["role"] != "Other":
        m["role"] = b["role"]
    if len(b.get("_text", "")) > len(m.get("_text", "")):
        m["_text"] = b["_text"]
    for k in ("when", "eligibility", "event_type"):
        if not m.get(k) and b.get(k):
            m[k] = b[k]
    m["audience"] = list(dict.fromkeys((a.get("audience") or []) + (b.get("audience") or [])))
    if b.get("kind") == "event":
        m["kind"] = "event"
        if a.get("seeded") and not b.get("seeded") and b.get("url"):
            m["url"] = b["url"]  # the scraped page-specific link beats the seed's hub link
    m["seeded"] = bool(a.get("seeded")) and bool(b.get("seeded"))  # a scraped hit means it's live, not just known
    return m


def merge_all(items: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for l in items:
        by[l["id"]] = merge_two(by[l["id"]], l) if l["id"] in by else l
    # collapse per-location duplicates (Amazon posts one listing per city)
    collapse = set(c.lower() for c in CFG.get("collapse_by_title", []))
    out, by_title = [], {}
    for l in by.values():
        if l["company"].lower() in collapse:
            k = (l["company"].lower(), l["title"].lower())
            if k in by_title:
                by_title[k] = merge_two(by_title[k], l)
                continue
            by_title[k] = l
        else:
            out.append(l)
    out = out + list(by_title.values())
    # events: "ETC" scraped from the hub and the seeded "Electronic Trading Challenge (ETC)" are the same thing
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower().replace("&", " and ")).strip()

    def names(t, company=""):  # "Electronic Trading Challenge (ETC)" -> {"electronic trading challenge etc", "electronic trading challenge", "etc"}
        ns = {norm(t)}
        m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", t)
        if m:
            ns |= {norm(m.group(1)), norm(m.group(2))}
        ns |= {norm(x) for x in re.findall(r"\(([^)]{2,30})\)", t)}  # acronyms anywhere: "… (ASDI) — formerly STEP"
        ns |= {norm(x) for x in re.split(r"\s+[—–]\s+|:\s+", t)}     # "FTTP — First-Year …" -> "fttp"
        cn = norm(company)
        if cn:  # "Jane Street FTTP" folds into "FTTP"
            ns |= {n[len(cn):].strip() for n in set(ns) if n.startswith(cn + " ")}
        ns |= {" ".join(sorted(n.split())) for n in set(ns)}         # "Microsoft Explore" == "Explore Microsoft"
        return {n for n in ns if len(n) >= 3}
    events = [l for l in out if l["kind"] == "event"]
    others = [l for l in out if l["kind"] != "event"]
    merged: list[dict] = []
    for l in sorted(events, key=lambda x: (not x["seeded"], -len(x["title"]))):  # seeds first, so scraped hits fold into them
        ln = names(l["title"], l["company"])
        hit = next((m for m in merged if m["company"] == l["company"] and not (m["seeded"] and l["seeded"]) and ln & names(m["title"], m["company"])), None)
        if hit:
            merged[merged.index(hit)] = merge_two(hit, l)
        else:
            merged.append(l)
    return others + merged


LIST_SOURCES = ("nufintech", "simplify", "speedyapply", "vansh")  # internship-only lists: no need for "intern" in the title


def passes(l: dict) -> bool:
    t = l["title"]
    if l.get("kind") == "event":
        blob = f"{t} {l.get('note') or ''} {l.get('eligibility') or ''}"
        if re.search(r"high school", blob, re.I) and not re.search(r"undergrad|university|college", blob, re.I):
            return False
        if CFG.get("undergrad_only") and l["level"] in ("phd", "masters", "mba"):
            return False
        if CFG.get("undergrad_only") and re.search(r"\bphd\b", t, re.I):
            return False
        if CFG.get("us_only") and l.get("us") == "non-US":
            return False
        return True
    if not any(s in l["sources"] for s in LIST_SOURCES) and not INTERN_RE.search(t):
        return False
    if CFG.get("us_only") and l.get("us") == "non-US":
        return False
    if l.get("watch_group") == "tech" and not re.search(r"quant", t, re.I):
        kws = CFG.get("tech_title_keywords", [])
        if kws and not any(re.search(k, t, re.I) for k in kws):
            return False
        if any(re.search(p, t, re.I) for p in CFG.get("tech_exclude_patterns", [])):
            return False
    for p in CFG.get("exclude_title_patterns", []):
        if re.search(p, t, re.I):
            return False
    if CFG.get("undergrad_only") and l["level"] in ("phd", "masters", "mba"):
        return False
    terms = CFG.get("cycle_terms") or []
    if terms and l.get("term") and l["term"] not in terms:
        return False
    if l["watch"] and not any(s in l["sources"] for s in ("simplify", "vansh", "nufintech")):
        kws = CFG.get("watch_title_keywords", [])
        if kws and not any(re.search(k, t, re.I) for k in kws):
            return False
    return True


def apply_manual_deadlines(items: list[dict]):
    for rule in CFG.get("manual_deadlines", []):
        comp = (rule.get("company") or "").lower()
        sub = (rule.get("title_contains") or "").lower()
        for l in items:
            if l["company"].lower() == comp and sub in l["title"].lower():
                if rule.get("deadline"):
                    l["deadline"], l["deadline_source"] = rule["deadline"], "manual"
                if rule.get("note"):
                    l["note"] = rule["note"]


def enrich(items: list[dict], cache: dict, limit: int):
    """Fetch posting text for listings lacking a deadline (bounded per run, cached forever)."""
    done = 0
    log(f"looking for deadlines in up to {limit} postings (Greenhouse text already checked)…")
    for l in items:
        if l.get("deadline") or not l.get("url") or l.get("status") == "closed":
            continue
        c = cache.get(l["id"])
        if c is not None:
            for k in ("deadline", "term"):
                if c.get(k) and not l.get(k):
                    l[k] = c[k]
                    if k == "deadline":
                        l["deadline_source"] = "posting"
            if c.get("level") and l["level"] == "unknown":
                l["level"] = c["level"]
            continue
        if not (l["watch"] or "nufintech" in l["sources"]) or done >= limit:
            continue
        text = l.get("_text") or ""
        try:
            if len(text) < 400:
                r = SESSION.get(l["url"], timeout=45, headers={"Accept": "text/html,*/*"})
                text = strip_html(r.text) if r.ok else ""
            if len(text) < 800:
                text = strip_html(jina_read(l["url"]))
                time.sleep(3.5)
        except Exception as e:
            vlog(f"  enrich: {l['company']} {l['title']} -> {e}")
        done += 1
        if len(text) < 200:
            continue  # fetch failed or page is JS-only: leave it uncached so the next run retries
        rec = {"fetched": NOW.isoformat(), "deadline": extract_deadline(text), "term": extract_term(text[:4000]),
               "level": None}
        if re.search(r"(currently|must be) (enrolled|pursuing)[^.]{0,40}\b(ph\.?d|doctoral)\b", text, re.I):
            rec["level"] = "phd"
        cache[l["id"]] = rec
        if rec["deadline"]:
            l["deadline"], l["deadline_source"] = rec["deadline"], "posting"
        if rec["term"] and not l.get("term"):
            l["term"] = rec["term"]
        if rec["level"] and l["level"] == "unknown":
            l["level"] = rec["level"]
    return done


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def notify(new: list[dict], dashboard_url: str):
    lines = []
    for l in new[:12]:
        due = f" — due {l['deadline']}" if l.get("deadline") else ""
        tag = f" ({l.get('event_type') or 'event'})" if l.get("kind") == "event" else ""
        lines.append(f"{l['company']}: {l['title']}{tag}{due}")
    if len(new) > 12:
        lines.append(f"…and {len(new) - 12} more")
    body = "\n".join(lines)
    title = f"{len(new)} new quant internship{'s' if len(new) != 1 else ''}"
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=30,
                          headers={"Title": title, "Click": dashboard_url, "Tags": "chart_with_upwards_trend"})
            log("  ntfy sent")
        except Exception as e:
            log(f"  ntfy failed: {e}")
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"content": f"**{title}**\n{body}\n{dashboard_url}"[:1950]}, timeout=30)
            log("  discord sent")
        except Exception as e:
            log(f"  discord failed: {e}")


def build_dashboard(payload: dict):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(DASH_PATH, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", blob))


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", help="comma-separated subset, e.g. simplify,nufintech")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose
    load_config()

    only = set(args.sources.split(",")) if args.sources else None
    srcs = CFG["sources"]
    status, raw, ctx = {}, [], {}

    def run(name, fn):
        if not srcs.get(name, {}).get("enabled", False) or (only and name not in only):
            status[name] = {"ok": None, "count": 0, "error": "disabled"}
            return
        t0 = time.time()
        try:
            res = fn(srcs[name], ctx)
            if isinstance(res, tuple):
                res, extra = res
                ctx.update(extra)
            raw.extend(res)
            err = "; ".join(ctx.pop("partial_errors", [])) or None
            status[name] = {"ok": True, "count": len(res), "error": err, "secs": round(time.time() - t0, 1)}
            log(f"{name}: {len(res)} listings" + (f" (partial: {err})" if err else ""))
        except Exception as e:
            status[name] = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:300], "secs": round(time.time() - t0, 1)}
            log(f"{name}: FAILED {type(e).__name__}: {e}")

    run("nufintech", lambda c, x: src_nufintech(c))
    run("simplify", src_simplify)
    run("vansh", lambda c, x: src_simplify(c, x, name="vansh"))  # Vansh & Ouckah list — same schema as Simplify
    run("speedyapply", src_speedyapply)
    run("greenhouse", src_greenhouse)
    run("janestreet", src_janestreet)
    run("amazon", src_amazon)
    run("lever", src_lever)
    run("ashby", src_ashby)
    run("workday", src_workday)
    run("microsoft", src_microsoft)
    run("pages", src_pages)
    run("events", src_events)
    run("underclassmen", src_underclassmen)

    merged = merge_all(raw)
    kept = [l for l in merged if passes(l)]
    log(f"merged {len(merged)} -> kept {len(kept)} after filters")

    cache = load_json(CACHE_PATH, {})
    if not args.no_enrich:
        n = enrich(kept, cache, int(CFG.get("enrich_limit_per_run", 15)))
        log(f"enriched {n} postings for deadlines")
    apply_manual_deadlines(kept)
    for l in kept:  # extract_deadline stores raw text ("Aug 28, 2026"); the dashboard and passed-check need ISO
        if l.get("deadline") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", l["deadline"]):
            d = parse_date_str(l["deadline"])
            if d:
                l["deadline"] = d.isoformat()
    save_json(CACHE_PATH, cache)

    seen = load_json(SEEN_PATH, {})
    first_run = not seen
    window = dt.timedelta(hours=int(CFG.get("new_window_hours", 48)))
    new_now = []
    for l in kept:
        fs = seen.get(l["id"])
        if not fs:
            fs = seen[l["id"]] = NOW.isoformat()
            if l.get("status") != "closed":  # expected-to-return rows are context, not news
                new_now.append(l)
        l["first_seen"] = fs
        l["is_new"] = (not first_run) and (NOW - dt.datetime.fromisoformat(fs)) < window
        l["deadline_passed"] = bool(l.get("deadline")) and l["deadline"] < TODAY.isoformat()
    live_ids = {l["id"] for l in kept}
    seen = {k: v for k, v in seen.items() if k in live_ids or (NOW - dt.datetime.fromisoformat(v)).days < 180}
    save_json(SEEN_PATH, seen)

    kept.sort(key=lambda l: l.get("date_posted") or "", reverse=True)
    kept.sort(key=lambda l: (l["deadline_passed"], l.get("deadline") is None, l.get("deadline") or ""))
    for l in kept:
        l.pop("_text", None)

    notes = ctx.get("notes", {})
    watch_summary = []
    for w in WATCH:
        n = sum(1 for l in kept if l["watch"] == w and l["kind"] == "internship")
        ev = sum(1 for l in kept if l["watch"] == w and l["kind"] == "event")
        info = notes.get(w) or {}
        watch_summary.append({"name": w, "group": WATCH_GROUP.get(w, "quant"), "count": n, "events": ev, "website": info.get("website"), "note": info.get("notes")})

    def gh_repo(u):
        m = re.match(r"https?://(?:raw\.githubusercontent\.com|codeload\.github\.com|github\.com)/([^/]+)/([^/]+)", u or "")
        return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None

    s = CFG.get("sources", {})
    source_links = {"nufintech": gh_repo(s.get("nufintech", {}).get("zip_url")),
                    "simplify": gh_repo(s.get("simplify", {}).get("url")),
                    "vansh": gh_repo(s.get("vansh", {}).get("url"))}
    for name, u in s.get("speedyapply", {}).get("lists", {}).items():
        source_links.setdefault("speedyapply", gh_repo(u))
        source_links[f"speedyapply:{name}"] = gh_repo(u)
    for name, rc in s.get("underclassmen", {}).get("repos", {}).items():
        source_links.setdefault("underclassmen", gh_repo(rc.get("url")))
        source_links[f"underclassmen:{name}"] = gh_repo(rc.get("url"))
    source_links = {k: v for k, v in source_links.items() if v}

    payload = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "source_links": source_links,
        "schedule_hours": int(os.environ.get("SCHEDULE_HOURS", "12")),
        "config": {"cycle_terms": CFG.get("cycle_terms"), "undergrad_only": CFG.get("undergrad_only"), "us_only": CFG.get("us_only")},
        "counts": {"total": sum(1 for l in kept if l["kind"] == "internship"), "events": sum(1 for l in kept if l["kind"] == "event"),
                   "with_deadline": sum(1 for l in kept if l.get("deadline")),
                   "new": sum(1 for l in kept if l["is_new"]), "watchlist": sum(1 for l in kept if l["watch"]),
                   "new_this_run": 0 if first_run else len(new_now)},
        "first_run": first_run,
        "sources": status,
        "watchlist": watch_summary,
        "listings": kept,
    }
    save_json(OUT_PATH, payload)
    build_dashboard(payload)
    log(f"wrote {OUT_PATH} and {DASH_PATH}: {len(kept)} listings, {len(new_now)} new this run")

    if new_now and not args.no_notify and not first_run:
        notify(new_now, os.environ.get("DASHBOARD_URL", ""))
    elif first_run:
        log("first run: baseline saved, no notification sent")


if __name__ == "__main__":
    main()
