# Quant intern finder

Searches every 12 hours for undergrad-eligible quant internships — plus software engineering internships at any
company (`include_all_swe`) — sorts them by deadline, publishes a dashboard, and pings your phone when something new appears.

```
sources ──► normalize ──► merge/dedupe ──► filter (undergrad, current cycle)
        ──► pull deadlines from posting text ──► sort by deadline ──► docs/index.html + data/internships.json ──► notify new
```

**Sources** (each one is isolated; a failure shows up as "failed" on the dashboard and never blocks the rest)

| key | what | notes |
|---|---|---|
| `nufintech` | [northwesternfintech/2027QuantInternships](https://github.com/northwesternfintech/2027QuantInternships) YAML data | curated quant firms, role codes (QR/QT/QD/SWE/HW) |
| `simplify` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) `listings.json` | "Quant" and Software categories (`swe_categories`) + every watchlist company; has degree and term fields |
| `vansh` | [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) (Vansh & Ouckah community list) `listings.json` | same schema as `simplify` but no categories, so software roles are picked by `swe_title_pattern`; overlapping postings merge, unique ones add |
| `speedyapply` | [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) and [2027-AI-College-Jobs](https://github.com/speedyapply/2027-AI-College-Jobs) READMEs | updated daily; adds hourly pay and posting age. Every row of the lists in `swe_lists` (the SWE list); from the AI list, the Quant section, watchlist / quant-firm rows and software titles |
| `greenhouse` | public Greenhouse job-board API | Jane Street (incl. their hidden `jshiddenevents` programs board — BRIDGE, WiSE, … with stated deadlines), HRT, Anthropic, Point72, DRW, IMC, Optiver, … posting text is included, so deadlines get extracted |
| `amazon` | amazon.jobs search API | US intern roles matching research / science / quant / SDE |
| `pages` | JS-rendered career pages read through [Jina Reader](https://r.jina.ai) | Citadel, Citadel Securities, Google. This is the same free backend Agent Reach routes "read web page" through — used directly, no agent needed |
| `janestreet` | Jane Street's own feed | off by default; their Greenhouse board covers it |
| `lever`, `ashby`, `workday`, `microsoft` | public job-board APIs | Palantir (Lever), OpenAI & Cohere (Ashby), NVIDIA (Workday), Microsoft (careers search API) |
| `events` | company "programs & events" pages read through Jina Reader, plus a seeded list of known recurring programs | datathons, insight days, trading challenges, fellowships, ambassador programs — things you apply to that aren't internships. Shown in their own section |
| `underclassmen` | three community GitHub lists of freshman/sophomore opportunities: [Jose-Gael-Cruz-Lopez/underclassmen-opportunities](https://github.com/Jose-Gael-Cruz-Lopez/underclassmen-opportunities) (README + ARCHIVE.md), [LuisaE/opportunities](https://github.com/LuisaE/opportunities), [zapplyjobs/underclassmen-internships](https://github.com/zapplyjobs/underclassmen-internships) | live postings (Optiver, Scale AI, …) join the internship list; recurring programs (Explore, Launch, JSIP, externships, ambassador roles) land in the events section and fold into the seeds. Rows the lists mark closed / opens-soon keep their status and last-known deadline instead of being dropped. Only programs from watchlist companies and known quant firms are kept, and tech-group ones must clear `tech_title_keywords` — set `"keep": "all"` to widen. Sections kept per repo are set in `config.json`; scholarship/HS sections are off by default |
| `agentreach` | [Agent Reach](https://github.com/Panniantong/Agent-Reach) CLI | LinkedIn job search, plus web search (Exa, free, no key) for single Greenhouse / Lever / Ashby postings at firms not in your board lists. A hit for a role another source already has folds into that row (company + title match) and just adds its source tag, so it only adds rows for roles nothing else found. Search indexes keep dead postings for years, so hits older than `max_age_days` or with no current-cycle term/year are dropped. Needs the `agent-reach` command — see [Agent Reach](#agent-reach-linkedin--web-search) |

## Easiest: the desktop app (no Terminal after the first launch)

Double-click **`Quant Finder.command`**. The first launch sets up Python (about a minute), then your browser opens
`http://127.0.0.1:8765` with:

- **Run search now** — runs the finder and shows each source reporting in a popup.
- **Click any listing** — a popup with deadline, locations, term, level, sources, an **Open posting** link,
  and buttons to **★ Star**, **✓ Applied**, or **Hide** it, plus a notes box. The **Show** menu filters by those.
- **Settings** — watchlist, terms to keep, undergrad-only, how often to run, Mac notifications, optional ntfy/Discord.
- While the window is open it re-runs on the schedule you set (default every 12 h) and posts a macOS notification
  when it finds listings it hasn't seen before.

If macOS says the file "can't be opened because it is from an unidentified developer", right-click it → **Open** once.
If it opens in a text editor instead, run `chmod +x "Quant Finder.command"` in the folder once.

**Run at login, in the background** (so you never have to double-click again while the Mac is awake):

```bash
source .venv/bin/activate && python app.py --install-login-item     # undo: --uninstall-login-item
```

The dashboard then lives at http://127.0.0.1:8765 whenever the Mac is on. A closed laptop doesn't run anything —
for that, use GitHub Actions below.

## Runs even with the laptop closed (GitHub Actions)

1. Create a new GitHub repo and push this folder to it (`main` branch).
2. **Settings → Pages → Source: Deploy from a branch → `main` / `/docs`.** The dashboard will live at
   `https://<you>.github.io/<repo>/` (star/applied/hide marks are kept in that browser only).
3. **Actions tab → "quant intern finder" → Run workflow.** The first run only records a baseline; it never notifies.
4. Optional push notifications — **Settings → Secrets and variables → Actions → New repository secret**:
   - `NTFY_TOPIC`: install the [ntfy](https://ntfy.sh) app, subscribe to a topic name nobody would guess
     (e.g. `gj-quant-9f3k2`), put that name here. Free, no account.
   - `DISCORD_WEBHOOK_URL`: a channel webhook, if you'd rather get it in Discord.

Schedule lives in `.github/workflows/finder.yml`: `17 */12 * * *` is every 12 h; `17 6 * * *` is once a day at 06:17 UTC.
GitHub may delay scheduled runs by some minutes at busy times, and pauses schedules on repos with no commits for 60 days —
the bot's own commits keep it alive, and re-enabling is one click in the Actions tab if it ever happens.

## Agent Reach (LinkedIn + web search)

The workflow installs a pinned, checksum-verified Agent Reach release before each run (`AGENT_REACH_VERSION` /
`AGENT_REACH_SHA256` in `finder.yml`; to upgrade, change both — `shasum -a 256 agent-reach.zip` gives the hash).
For the desktop app and command line on your Mac:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # skip if `uv --version` already works
uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip
```

Without it the `agentreach` source shows "failed" and every other source runs as usual. `sources.agentreach` in `config.json`:

- `searches` — `{"channel": "linkedin", "query": …, "location": …}` or `{"channel": "web", "query": …}`.
- `linkedin_backend: "public-web"` searches LinkedIn through the public search index: no login, works in Actions.
  Set it to `""` on the Mac after `agent-reach add linkedin` to search signed in (fresher, more complete). Agent Reach
  copies the LinkedIn login from your Chrome, and LinkedIn can restrict accounts used for automation — a secondary account is safer.
- `max_age_days`, `exclude_companies` (job-reposting aggregators), `limit`, `timeout`.

## Command line

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python finder.py                 # one full run → data/ + docs/index.html
python finder.py --sources simplify,nufintech --no-notify   # quick test
python app.py                    # the desktop app
```

`run-local.sh` is a cron-friendly wrapper for the bare finder (reads `NTFY_TOPIC` etc. from a `.env` file next to it):
`17 */12 * * * /path/to/quant-intern-finder/run-local.sh >> /tmp/quant-finder.log 2>&1`.

## Tune it — `config.json`

- `watchlist_groups.quant` / `.tech` — companies pinned at the top with counts (0 is shown too, so you can see when a firm has nothing open). Tech-group companies (Meta, Apple, Microsoft, NVIDIA, Netflix, OpenAI, Y Combinator, Palantir, Tesla, …) only show roles matching `tech_title_keywords` (software, ML, research, data, quant) minus `tech_exclude_patterns` (hardware, mechanical, sales, …), so a company that posts 200 internships doesn't bury the quant list.
- `sources.lever` / `sources.ashby` / `sources.workday` / `sources.microsoft` — direct job-board APIs for Palantir, OpenAI, Cohere, NVIDIA and Microsoft. Add any Lever/Ashby company by its slug, any Workday site by host/tenant/site (look at the URL of its careers page).
- `include_all_swe` — software internships from every company, not just quant firms and the watchlist: Simplify's
  `swe_categories`, and titles matching `swe_title_pattern` (minus `swe_exclude_pattern`: hardware, chip, sales…) from
  the lists without categories. Set it to `false` to go back to quant + watchlist only.
- Changing what's tracked (any source, filter or watchlist setting) makes the next run record the rows it newly brings in
  quietly — no wall of NEW badges and no notification for the backlog; genuinely new postings after that ping as usual.
- `cycle_terms` — listings whose term is known and *not* in this list are dropped. Remove `Fall 2026` to hide off-cycle roles.
- `undergrad_only` — drops titles/degree fields marked PhD, Master's, MBA. "BS/MS" counts as eligible. Unknown level is kept and shown as "not stated" in the popup.
- `us_only` — drops listings whose every location is outside the US (London, Paris, Toronto, …). Listings with a US office among several are kept; listings with no location stated are kept and badged.
- `quant_firms` — firms whose listings are pulled from the Simplify list in every category (SWE, AI/ML, hardware), not just the Quant category. Firms in the Northwestern list and any Greenhouse board you add count automatically.
- `sources.greenhouse.boards` — add any company on Greenhouse: open one of its postings, the token is the segment in
  `job-boards.greenhouse.io/<token>/jobs/…` or `?for=<token>` in the embedded board URL.
- `sources.pages.pages` — any JS-heavy listing page: give `company`, `url`, and a `link_pattern` regex that matches its posting links.
- `sources.events.pages` — programs pages to scrape (`link_pattern` scopes which links count; `require_keyword: false` for pages that are nothing but programs).
- `sources.events.seeded` — recurring programs shown even when a scrape fails (Jane Street INSIGHT/FOCUS/SEE/ETC, Citadel datathons and invitationals, HRT WiTTI, Google STEP/Student Researcher/GSoC, Claude Campus, …). A scraped hit with the same name folds into the seed and takes over its link and dates. Add your own with `company`, `title`, `url`, `type`, `eligibility`, `note`.
- `manual_deadlines` — for deadlines announced elsewhere (info sessions, emails):
  ```json
  {"company": "Jane Street", "title_contains": "Trader", "deadline": "2026-10-15", "note": "from campus info session"}
  ```
- `exclude_title_patterns`, `watch_title_keywords` — noise control for the big companies (Amazon/Google post hundreds of intern roles).
- `enrich_limit_per_run` — how many postings without a deadline get their page fetched per run (results are cached in `data/cache.json`).
- `exclude_event_patterns` — regexes for program/event titles to hide (curated seeds are never hidden). Ships with list
  filler like "Jane Street Programs" and high-school-only programs.
- `link_check` — links from community lists and scraped program pages are re-checked every `ttl_hours`; one that 404s or
  now bounces to a generic careers page (a retired program page) drops its row, so a seeded program falls back to
  "known program" instead of linking somewhere useless. Blocked or timed-out checks never drop anything. `--no-linkcheck` skips it.

**What keeps the list clean:** the same posting arriving from several sources (board, Simplify, vansh short links,
speedyapply, LinkedIn) is shown once, keyed by its job id where the URL has one and by company + title + term otherwise;
board jobs like "Technical Program Manager" are never treated as student programs; and menu, breadcrumb, image and
off-site links on scraped program pages are skipped.

## What "deadline" means here

Most quant firms don't publish one — they close roles when the class fills. So the **Deadline ladder** only holds postings whose
text states a date (or a `manual_deadlines` entry), and everything else sits under **Rolling**, newest first.
Anything whose deadline has already passed, or that a source list marks closed or opens-soon, moves to
**Not currently open · expected to return** at the bottom — recurring programs usually come back next cycle, so they're kept
with their last-known deadline instead of vanishing; when a new cycle's date appears they move back up on their own.
Confirm dates and eligibility on the posting itself before planning around them.

## Files

```
Quant Finder.command    double-click launcher for the desktop app
app.py                  desktop app: serves the page, schedules runs, stores marks, Mac notifications
finder.py               the whole search pipeline
config.json             what to track and how to filter (Settings edits this)
templates/dashboard.html  the page; app.py serves it live, finder.py bakes data into docs/index.html
data/internships.json   latest results
data/marks.json         your star / applied / hidden marks and notes
data/seen.json          first-seen timestamps → NEW badges and notifications
data/cache.json         fetched deadline/term per posting
.github/workflows/finder.yml   the cloud schedule
```
