# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This directory is one piece of the larger `my-server-conf` mono-repo (see the
repo-root `CLAUDE.md` for the Pi provisioning system as a whole). It runs a
fork of [AdUki/ai-marketplace-monitor](https://github.com/AdUki/ai-marketplace-monitor)
as a systemd **user** service (`aimm.service`) that scores Facebook
Marketplace listings with an LLM and pushes ntfy alerts for genuine deals.

The fork exists because upstream is missing ~11 fixes this deployment
depends on (login, notifications, geo-scoping, AI scoring). `install.sh`
`pip install`s the fork straight from GitHub into a local venv — there is no
local clone of the fork's git repo on this machine, and this directory's
`.git` is the `my-server-conf` repo, not the fork's.

**Important gotcha**: `device_facts.py` and `benchmark_db.py` in this
directory are full copies of same-named modules that live inside the fork's
own package (`ai_marketplace_monitor/device_facts.py` and
`.../benchmark_db.py`, verified byte-identical after install). They are
developed and tested here, but **editing them in this directory has no
runtime effect** — the venv only ever runs what `pip install` pulled from
the fork's GitHub repo. Getting a change live means porting it into the fork
repo (not present locally) and then running `install.sh --upgrade`. Nothing
in this repo automates that sync.

## Running

```bash
./install.sh              # idempotent: venv, fork install, chromium, config symlink, secrets scaffold, systemd unit
./install.sh --upgrade    # force-reinstall the fork at its latest main commit (plain --upgrade is a no-op — see comment in install.sh)
./run.sh                  # sources ~/.ai-marketplace-monitor/secrets.env, execs the tool --headless --webui-host 0.0.0.0
./run.sh --warm-facts     # drain the device-facts queue instead of monitoring
./warm-loop.sh [rounds]   # repeatedly call --warm-facts with pauses, for bulk-draining a backlog (default 40 rounds)
```

Service management (systemd **user** units, not root):
```bash
systemctl --user restart aimm            # main monitor loop
systemctl --user status aimm-warm-facts  # oneshot, fired every 15 min by aimm-warm-facts.timer
journalctl --user -u aimm -f
```

Runtime state — cache, saved Facebook session, secrets — lives in
`~/.ai-marketplace-monitor/`, **not** in this directory. `config.toml` here
is symlinked to `~/.ai-marketplace-monitor/config.toml` so there is one
source of truth (the tool always reads that fixed path). Never write
Facebook credentials, API keys, or the ntfy topic into the committed
`config.toml` — they're referenced as `${FACEBOOK_USERNAME}` etc. and
supplied at runtime via `~/.ai-marketplace-monitor/secrets.env` (copied from
`secrets.env.example`, gitignored, `chmod 600`).

The web UI (`:8467`) is deliberately not reverse-proxied: its login gate
authenticates against the monitored Facebook account's own credentials, so
it stays LAN/Tailscale-only.

## Testing

No pytest — plain `unittest`, run directly with the venv's stdlib-only
interpreter:

```bash
.venv/bin/python test_device_facts.py       # or: python3 test_device_facts.py (any interpreter, stdlib only)
.venv/bin/python test_classification.py
.venv/bin/python test_benchmark_db.py
.venv/bin/python test_confirmation.py
.venv/bin/python test_ntfy_send.py
.venv/bin/python test_verdict_source.py
```

To run one test: `python3 test_device_facts.py TestQualityScore.test_weak_phone_scores_low`.

Network is stubbed by default everywhere; live-network cases are opt-in with
`LIVE=1 python3 test_device_facts.py`.

Two different things get tested here, importing from two different places:
- `test_device_facts.py` and `test_benchmark_db.py` import the **local**
  `device_facts.py` / `benchmark_db.py` in this directory (`sys.path.insert`
  on the directory itself) — these are unit tests for code being developed
  here, ahead of being ported into the fork.
- `test_classification.py`, `test_confirmation.py`, `test_ntfy_send.py`,
  `test_verdict_source.py` import `ai_marketplace_monitor.*` from the
  **installed venv package** — these are regression tests that pin specific
  behavior of the fork itself (each was written after a real bad
  notification shipped; the docstring at the top of each file names the
  incident). Re-run this whole suite after `install.sh --upgrade` pulls new
  fork commits, since nothing else verifies the fork didn't regress.

`test_classification.py`'s corpus is real listing titles pulled from the
monitor's own queue, not invented examples — invented ones were "too tidy"
and missed the classification bugs real listings hit.

## Architecture

**Two extension modules** (source lives here, mirrored into the fork):

- `device_facts.py` — resolves hard specs (chip AnTuTu-equivalent, RAM,
  official LineageOS support) for a phone/laptop model once, then caches
  forever, so per-listing AI evaluation gets facts instead of guessing them.
  Sources: LineageOS's build API (official builds only — absence there does
  NOT mean unofficial builds don't exist), Groq's `groq/compound[-mini]`
  agentic models for live web search, hand-maintained `OVERRIDES` for facts
  the automated sources get wrong. A `--warm-facts` lookup is never allowed
  to happen inline during evaluation (network calls are slow and share a
  rate-limited budget with the monitor); a cache miss instead gets queued to
  `device_facts_pending.json` and resolved later by `warm_cache()`.
  `deterministic_verdict()` is the arithmetic-only fast path: when specs and
  price are both fully known, it decides `deal`/`reject` outright without
  spending an AI call, and tags the verdict with `SCRIPT_VERDICT_NAME` so
  `notification.py` labels it "Script" rather than naming a model.
- `benchmark_db.py` — bulk CPU/GPU/phone benchmark tables scraped once from
  PassMark (`--update-benchmarks`) and matched offline against listing text
  via a fuzzy device/component matcher (`match_device`, `match_component`).
  Whole finite population, downloaded once rather than asked about
  one-model-at-a-time — this is what `device_facts.py` calls into for
  `quality_score()`.

**Fork behaviors this deployment relies on** (exercised only by the
regression tests above, since their source isn't in this repo):
- A "second opinion" step (`confirm_by_ai` in the fork's `monitor.py`): a
  rating-5 verdict from one AI service gets checked against a second
  service before a notification fires; disagreement drops the score. Added
  after one service invented a resale value and nearly sent a bad alert.
- ntfy send is retry-wrapped but must stay idempotent-safe even though ntfy
  has no idempotency key (`test_ntfy_send.py`).

**`config.toml`** is the single source of truth for what gets monitored —
search phrases, keyword pre-filters (cut junk and AI-quota burn before the
model ever sees a listing), the graduated discount-curve prompt, and
per-item `extra_prompt` overrides (e.g. `item.dryer` replaces the phone-
resale prompt entirely, since its criteria are unrelated). Every non-obvious
config value carries an inline comment explaining a real observed failure
(a broken Facebook filter combination, a rate limit discovered from a 429
body, a scam pattern that fired on a legitimate cheap listing) — read those
comments before changing a value; several encode "already tried this,
reverted for X reason."

**AI service chaining**: `config.toml`'s `[ai.*]` sections are tried in
order, falling through on error/quota-exhaustion — each Groq model has an
independent daily quota, so listing several multiplies the effective daily
ceiling rather than sharing one pool. Same idea for the Gemini fallback
tier, each with its own 20/day limit.

## Things to keep consistent when editing

- Never write secrets (Facebook credentials, API keys, ntfy topic) into
  `config.toml` or `secrets.env.example` — only `${VAR}` references and
  blank placeholders belong in committed files.
- A change to `device_facts.py`/`benchmark_db.py` only affects the local
  test suite until it's ported into the fork and reinstalled — don't assume
  editing the file here changes what `aimm.service` actually runs.
- `debug_*.html` and `__pycache__/*.pyc` files accumulate in this directory
  at runtime (Playwright debug dumps, bytecode cache) and are currently
  **not** gitignored at the repo root — check `git status` before staging
  broadly here.
