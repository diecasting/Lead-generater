---
name: b2b-lead-filter-engine
description: |-
  This skill should be used when building, improving, or auditing a B2B
  lead-generation or web-scraping pipeline that searches the web for potential
  customers and needs to (a) discard competitor suppliers (die casters, CNC
  shops, injection molders, foundries, machine shops) that advertise
  themselves, and (b) keep only real buyers, outsourcing firms, brand owners,
  and RFQ posters. It provides a drop-in, dependency-free Python module
  (lead_filter_engine.py) implementing anti-competitor email/text detection and
  a strict buyer-intent gate that uses a negative lookahead to exclude supplier
  reverse-solicitation ("request a quote from us"), plus a one-command
  initializer that scaffolds the module and its test suite into any project.
agent_created: true
---

# B2B Lead Filter Engine (Anti-Competitor + Buyer Gate)

A reusable, zero-dependency (stdlib `re` / `sys` only) Python engine that
separates **real buyers** from **competitor suppliers** in raw web-search lead
data. Originally hardened in a production GitHub-Actions lead pipeline
(diecasting/Lead-generater) and validated with 90+ passing tests.

## When to use

- The user is building or fixing a B2B lead scraper / search bot and the output
  is polluted with peer manufacturers, foundries, or machine shops instead of
  actual buyers.
- The user asks to "filter out competitors", "keep only buyers / RFQ posters /
  outsourcing firms", or "exclude supplier self-advertising".
- The user wants a drop-in `lead_filter_engine.py` plus tests scaffolded into a
  new project in one step.

## What the engine provides (3 core capabilities)

1. **Competitor-email detection** — `is_competitor_email()` flags contact
   emails whose local-part or domain exposes a manufacturing identity
   (`yongzhucasting@…`, `sales@abc-machining.com`). Strong signals
   (casting/foundry/machining/cnc/tooling/…) always trigger; weak signals
   (parts/tech/factory/…) trigger only on non-free-mail domains to avoid
   hitting `acmetech@gmail.com`.
2. **Competitor-text blacklist** — `is_competitor()` / `filter_competitors()`
   scan title + snippet + URL for self-ad phrases (`we are a manufacturer`,
   `our foundry`, `casting capabilities`, `request a quote from us`, …) and a
   high-precision regex requiring `we/our` + supplier noun without an
   intervening buyer verb.
3. **Strict buyer-intent gate** — `is_true_buyer()` / `passes_buyer_gate()`
   match explicit buyer actions (`we are looking for a supplier`, `seeking a
   manufacturer`, `our company needs`, `need to outsource`, `requesting a
   quote`) and real procurement posts (RFQ id / drawing / CAD / STEP demand).
   A negative lookahead `(?! from)` excludes supplier reverse-solicitation
   ("request a quote from us").

Helper: `filter_competitor_emails()` (post-enrichment email stripping) and the
one-shot `filter_leads()` pipeline (text anti-competitor → optional buyer gate).

## How to use in a new project

### Option A — one-command scaffold (preferred)

Run the bundled initializer, pointing at the target project root:

```bash
python "~/.workbuddy/skills/b2b-lead-filter-engine/scripts/init_lead_filter.py" /path/to/new/project
```

The script copies `lead_filter_engine.py` and `tests/test_lead_filter_engine.py`
into the project, then runs the module self-test and (if available) pytest.
The skill directory is `~/.workbuddy/skills/b2b-lead-filter-engine`; the agent
can resolve it from the skill metadata.

### Option B — manual copy

Copy `assets/lead_filter_engine.py` to the project root and
`assets/test_lead_filter_engine.py` to `PROJECT_ROOT/tests/`, then:

```bash
python lead_filter_engine.py                      # self-test (no deps)
pytest tests/test_lead_filter_engine.py -q        # if pytest installed
```

## Integration example

```python
from lead_filter_engine import (
    is_competitor_email, is_competitor, is_true_buyer,
    filter_competitors, filter_competitor_emails, filter_leads,
)

# 1) A raw search result is a dict with title/snippet/keyword/url (+ emails later)
raw = [
    {"url": "https://acme-cast.com", "title": "We are a manufacturer of die casting",
     "snippet": "Our foundry offers ISO certified casting", "keyword": "looking for die casting supplier"},
    {"url": "https://buyer-co.com/rfq", "title": "We are looking for a die casting supplier",
     "snippet": "Our company needs custom aluminum parts", "keyword": "looking for die casting supplier"},
]

# 2) One-shot pipeline: drop competitors, keep only real buyers / RFQ posters
survivors = filter_leads(raw)          # defaults: require_buyer_intent=True

# 3) After fetching pages and extracting contact emails, strip competitor emails
leads = enrich_with_emails(survivors)  # your own fetcher
leads = filter_competitor_emails(leads)
```

## Tuning the rules

All rules are deterministic constants at the top of `lead_filter_engine.py` —
no external API needed, easy to audit and test:

- `COMPETITOR_EMAIL_STRONG` / `COMPETITOR_EMAIL_WEAK` — email identity tokens.
- `FREE_EMAIL_DOMAINS` — whitelist so weak tokens don't flag free-mail buyers.
- `COMPETITOR_HARD_PHRASES` / `COMPETITOR_REGEX` — supplier self-ad text.
- `TRUE_BUYER_RE` / `RFQ_PLATFORM_RE` — genuine buyer-intent patterns.
  **Do not remove the `(?! from)` lookahead** in `TRUE_BUYER_RE`; it is what
  stops supplier reverse-solicitation from being mistaken for buyer intent.

When adding industry vocabulary, append to the relevant tuple and add a test in
`test_lead_filter_engine.py` (see its parametrized cases for the pattern).

## Notes / pitfalls

- The engine is language-agnostic in shape but the shipped phrases target
  English buyer/supplier text; for other languages extend the tuples/regex.
- `filter_competitor_emails` runs *after* email extraction. If every extracted
  email on a page is a competitor AND the page text is not clearly buyer-side,
  the whole lead is dropped (avoids keeping a supplier page with one stray
  buyer address).
- Keep `lead_filter_engine.py` as the single source of truth; import it from
  the project's main pipeline rather than duplicating the rules.
