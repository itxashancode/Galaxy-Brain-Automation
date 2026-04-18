<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d1117,25:0d2137,50:0a3d62,75:0d2137,100:0d1117&height=260&section=header&text=🧠%20Galaxy%20Brain%20Bot&fontSize=58&fontColor=58a6ff&animation=fadeIn&fontAlignY=38&desc=GitHub%20Discussions%20Automation%20%7C%20Built%20for%20Learning&descAlignY=58&descColor=8b949e&stroke=1c3a5e&strokeWidth=2" width="100%"/>

<br/>

<!-- Animated typing banner -->
<img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=700&size=22&duration=2500&pause=800&color=58A6FF&center=true&vCenter=true&width=680&lines=Auto-discovers+open+GitHub+Discussions;Generates+answers+via+18%2B+free+LLMs;Circuit+breakers+%2B+smart+rate+limiting;Tracks+your+Galaxy+Brain+badge+progress;Educational+research+tool+%E2%80%94+use+it+wisely" alt="Typing SVG" />

<br/><br/>

<!-- Core badges row 1 -->
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776ab?style=for-the-badge&logo=python&logoColor=white"/></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-22863a?style=for-the-badge&logo=opensourceinitiative&logoColor=white"/></a>
<a href="https://openrouter.ai"><img src="https://img.shields.io/badge/OpenRouter-18%2B%20Free%20Models-7c3aed?style=for-the-badge&logo=openai&logoColor=white"/></a>
<img src="https://img.shields.io/badge/Purpose-Educational%20Only-e3b341?style=for-the-badge&logo=bookstack&logoColor=white"/>

<br/>

<!-- Core badges row 2 -->
<img src="https://img.shields.io/badge/GitHub%20GraphQL-API%20v4-0969da?style=for-the-badge&logo=github&logoColor=white"/>
<img src="https://img.shields.io/badge/Circuit%20Breakers-Resilient%20Design-16a34a?style=for-the-badge"/>
<img src="https://img.shields.io/badge/Adaptive%20Rate%20Limiting-Smart%20Backoff-dc2626?style=for-the-badge"/>
<img src="https://img.shields.io/badge/Multi--Model%20AI-Round%20Robin%20Rotation-0ea5e9?style=for-the-badge"/>

<br/><br/>

<!-- Live stats counters (animated on GitHub) -->
<img src="https://img.shields.io/github/stars/itxashancode/Galaxy-Brain-Automation?style=social"/>
<img src="https://img.shields.io/github/forks/itxashancode/Galaxy-Brain-Automation?style=social"/>
<img src="https://img.shields.io/github/watchers/itxashancode/Galaxy-Brain-Automation?style=social"/>

</div>

---

> **⚠️ Heads up before you run anything.**
> This is an educational project. It demonstrates GitHub GraphQL automation, multi-model LLM orchestration, and how production-grade HTTP clients handle failure. Running it blind against real repos without reading the output could get your GitHub account flagged. Read GitHub's [Acceptable Use Policy](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) first. This isn't to cover bases — it's because the bot is powerful enough to do real damage if you're careless with it.

---

## What it actually does

<div align="center">

```
┌─────────────────────────────────────────────────────────────────┐
│                    🧠 GALAXY BRAIN BOT                          │
│                   High-Level Overview                           │
└─────────────────────────────────────────────────────────────────┘

  You run the bot
       │
       ▼
  ┌─────────────┐      GraphQL v4       ┌──────────────────────┐
  │  Discovery  │ ──────────────────▶  │  GitHub Discussions  │
  │  Engine     │ ◀──────────────────  │  (open threads)      │
  └─────────────┘   paginated results  └──────────────────────┘
       │
       ▼
  ┌─────────────┐     already answered?   ┌──────────────────┐
  │  Filter     │ ───────────────────────▶│  Stats JSON      │
  │  + Dedup    │ ◀───────────────────────│  (skip if found) │
  └─────────────┘                         └──────────────────┘
       │
       ▼
  ┌─────────────┐     prompt + context    ┌──────────────────┐
  │  LLM Layer  │ ──────────────────────▶│  OpenRouter API  │
  │  (18 models)│ ◀──────────────────────│  free tier       │
  └─────────────┘     generated answer   └──────────────────┘
       │
       ▼
  ┌─────────────┐   you confirm (y/n)
  │  Post /     │ ──────────────────────▶ GitHub Discussion reply
  │  Skip       │
  └─────────────┘
       │
       ▼
  ┌─────────────┐
  │  Track it   │ ──▶ galaxy_brain_stats.json
  │  + notify   │ ──▶ Discord / Slack webhook
  └─────────────┘
```

</div>

Galaxy Brain Bot finds open GitHub Discussion threads, generates answers using free LLMs through OpenRouter, and optionally posts them under your account. It then watches for accepted answers so you can track your [Galaxy Brain badge](https://docs.github.com/en/discussions/guides/finding-your-discussions) progress.

The interesting part isn't the badge farming. It's the infrastructure underneath: circuit breakers, adaptive rate limiting, key rotation, TTL caching, graceful shutdown. These are the patterns any production bot needs, and they're all here in readable Python.

---

## Architecture deep-dive

### How the pieces connect

```
┌──────────────────────────────────────────────────────────────────────┐
│                        SYSTEM ARCHITECTURE                           │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│   │ Shutdown     │    │ InMemory     │    │ CircuitBreaker        │  │
│   │ Handler      │    │ Cache        │    │ (per endpoint)        │  │
│   │              │    │              │    │                       │  │
│   │ SIGINT/TERM  │    │ TTL: 300s    │    │ threshold → open     │  │
│   │ sets flag    │    │ dict+stamps  │    │ timeout  → half-open │  │
│   └──────┬───────┘    └──────┬───────┘    └──────────┬───────────┘  │
│          │                  │                        │              │
│          └──────────────────┼────────────────────────┘              │
│                             ▼                                        │
│                    ┌────────────────┐                                │
│                    │  GitHub        │                                │
│                    │  GraphQL v4    │                                │
│                    │  Client        │                                │
│                    └───────┬────────┘                                │
│                            │                                         │
│          ┌─────────────────┼──────────────────┐                     │
│          ▼                 ▼                  ▼                     │
│   ┌─────────────┐  ┌─────────────┐  ┌──────────────┐               │
│   │ Discovery   │  │ Comment     │  │ Badge        │               │
│   │ (topics,    │  │ Reader      │  │ Progress     │               │
│   │  stars,     │  │ (avoids     │  │ Tracker      │               │
│   │  recency)   │  │  duplicates)│  │              │               │
│   └─────────────┘  └─────────────┘  └──────────────┘               │
│                             │                                        │
│                             ▼                                        │
│                    ┌────────────────┐                                │
│                    │  KeyManager    │                                │
│                    │  + ModelTracker│                                │
│                    │                │                                │
│                    │  round-robin   │                                │
│                    │  keys/models   │                                │
│                    └───────┬────────┘                                │
│                            │                                         │
│                            ▼                                         │
│                    ┌────────────────┐                                │
│                    │  OpenRouter    │                                │
│                    │  18+ free LLMs │                                │
│                    └────────────────┘                                │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### CircuitBreaker — the failure handler

This is the most underrated piece in the codebase. Without it, when an endpoint goes down, the bot hammers it until the session ends.

```
┌──────────────────────────────────────────────────────┐
│              CIRCUIT BREAKER STATE MACHINE           │
├──────────────────────────────────────────────────────┤
│                                                      │
│         requests                 N consecutive       │
│         succeed                  failures            │
│    ┌──────────────┐         ┌──────────────┐         │
│    │              │ ──────▶ │              │         │
│    │   CLOSED     │         │    OPEN      │         │
│    │ (normal ops) │ ◀────── │ (blocked)    │         │
│    │              │  probe  │              │         │
│    └──────────────┘ success └──────┬───────┘         │
│                                    │ timeout          │
│                                    ▼ expires          │
│                           ┌──────────────┐            │
│                           │  HALF-OPEN   │            │
│                           │ (let 1 probe │            │
│                           │  through)    │            │
│                           └──────────────┘            │
│                                                       │
│  Configurable:  threshold=5 failures                  │
│                 timeout=120 seconds                   │
└───────────────────────────────────────────────────────┘
```

Three states. Closed means everything's fine — requests go through. Open means something broke — requests are blocked without hitting the endpoint. Half-open means the timeout expired and one probe gets through. If it succeeds, back to closed. If it fails, back to open.

You can tune `CIRCUIT_BREAKER_THRESHOLD` and `CIRCUIT_BREAKER_TIMEOUT` in `.env`.

### Adaptive rate limiting

Not all rate limiting is equal. Respecting the headers the server sends back is smarter than just sleeping for a fixed amount.

```
┌─────────────────────────────────────────────────────┐
│           ADAPTIVE BACKOFF DECISION TREE            │
├─────────────────────────────────────────────────────┤
│                                                     │
│         API responds with 429 or 403                │
│                     │                               │
│                     ▼                               │
│         ┌─────────────────────┐                     │
│         │  Retry-After        │                     │
│         │  header present?    │                     │
│         └──────────┬──────────┘                     │
│               yes  │  no                            │
│          ┌─────────┴──────────┐                     │
│          ▼                    ▼                     │
│   sleep(Retry-After)   X-RateLimit-Reset            │
│          │             header present?              │
│          │                   │                      │
│          │           yes     │    no                │
│          │       ┌───────────┴────────┐             │
│          │       ▼                   ▼              │
│          │  sleep until          exponential        │
│          │  reset time           backoff            │
│          │                    (2^n seconds)         │
│          └────────────────────────┘                 │
│                     │                               │
│                     ▼                               │
│              retry request                          │
└─────────────────────────────────────────────────────┘
```

### Model rotation

The bot doesn't just call one model and give up if it's slow. It cycles through 18+ free models on OpenRouter, skipping any that return empty responses, 429s, or 404s.

```
┌────────────────────────────────────────────────────────┐
│              MODEL ROTATION FLOW                       │
├────────────────────────────────────────────────────────┤
│                                                        │
│  Model list (18+ free-tier models)                     │
│  [qwen3.6-plus, llama-3.3-70b, gemma-3-27b, ...]       │
│                │                                       │
│                ▼                                       │
│       ┌──────────────┐                                 │
│       │  Try model 1 │                                 │
│       └──────┬───────┘                                 │
│         good │   bad (empty / 429 / 404)               │
│              │         │                               │
│              ▼         ▼                               │
│        use answer  log failure                         │
│              │         │                               │
│              │         ▼                               │
│              │   try model 2 → model 3 → ...           │
│              │                                         │
│              ▼                                         │
│       ModelTracker records:                            │
│         - success count                                │
│         - failure count                                │
│         - average latency (ms)                         │
└────────────────────────────────────────────────────────┘
```

At the end of a session, `--models` shows you a table of which models worked, how often, and how fast.

---

## Features

### Core

- **Auto-discovery** — finds repos using topic tags, star counts, and activity recency. No hardcoded lists needed.
- **Smart deduplication** — saves answered discussion IDs to `galaxy_brain_stats.json` so the bot never double-posts across sessions.
- **CoC-aware filtering** — skips repos with codes of conduct that restrict automated participation (not perfect, but it tries).
- **Comment-aware** — reads existing replies before generating an answer so it doesn't pile on something already covered.

### Reliability

- **Circuit breakers** — per endpoint, configurable threshold and timeout (see diagram above).
- **Adaptive backoff** — reads `Retry-After`, `X-RateLimit-Reset`, and falls back to exponential delays when neither header is present.
- **Key rotation** — cycles multiple OpenRouter API keys when one hits a rate limit.
- **Model rotation** — tries 18+ free models in order and skips broken ones automatically.

### AI integration

- **18+ free models** via OpenRouter — Qwen, Llama, Gemma, Nemotron, and more.
- **Vision support** — detects if a model handles images and includes them from the discussion when relevant.
- **Link fetching** — pulls external URLs from discussion bodies for richer context before generating an answer.
- **Configurable answer length** — min/max character limits stop one-liner garbage from getting posted.

### Tracking

- **Badge progress** — shows accepted answer count and current tier toward the Galaxy Brain badge.
- **Stats by org** — breaks down your answers by repository owner.
- **Session model performance** — tracks which AI models succeed vs. fail and their latency.
- **Webhook notifications** — Discord and Slack alerts on new acceptances and session summaries.

---

## Galaxy Brain badge tiers

<div align="center">

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🥉 BRONZE    8 accepted answers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🥈 SILVER   16 accepted answers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🥇 GOLD     32 accepted answers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

The bot shows your current tier and how many accepted answers you need for the next one. Run `--stats` to check anytime.

</div>

---

## Installation

**Requirements:** Python 3.8+, a GitHub account, a free [OpenRouter](https://openrouter.ai) account.

```bash
# Clone the repo
git clone https://github.com/itxashancode/Galaxy-Brain-Automation.git
cd Galaxy-Brain-Automation

# Install dependencies
pip install -r requirements.txt

# Copy and fill in your credentials
cp .env.template .env
nano .env
```

`requirements.txt`:
```
requests>=2.31.0
rich>=13.7.0
python-dotenv>=1.0.0
```

---

## Configuration

Create a `.env` file in the project root. Every option has a comment explaining what it does.

```env
# ── GitHub ─────────────────────────────────────────────────────
# needs read:discussion and write:discussion scopes
GITHUB_TOKEN=ghp_your_token_here
GITHUB_USERNAME=your_github_username

# ── OpenRouter ─────────────────────────────────────────────────
# comma-separated if you have multiple keys
OPENROUTER_KEYS=sk-or-v1-your_key_here

# optional: override the model list (defaults to 18-model rotation)
# OPENROUTER_MODELS=qwen/qwen3.6-plus:free,meta-llama/llama-3.3-70b-instruct:free

# ── Webhooks (optional) ────────────────────────────────────────
DISCORD_WEBHOOK_URL=
SLACK_WEBHOOK_URL=

# ── Bot behavior ───────────────────────────────────────────────
MIN_REPO_STARS=10
MAX_ANSWERS_PER_SESSION=5
DELAY_BETWEEN_ANSWERS=5
AUTO_APPROVE_ANSWERS=false       # keep this false until you trust the output

# ── Discovery ──────────────────────────────────────────────────
DISCOVERY_TOPICS=python,javascript,open-source,programming
DISCOVERY_MIN_STARS=5
DISCOVERY_MAX_REPOS=50

# ── Answer quality ─────────────────────────────────────────────
ANSWER_MIN_CHARS=120             # prevents one-liner garbage
ANSWER_MAX_CHARS=900

# ── Performance ────────────────────────────────────────────────
CACHE_TTL_SECONDS=300
CIRCUIT_BREAKER_THRESHOLD=5
CIRCUIT_BREAKER_TIMEOUT=120
RECENT_HOURS=24
```

**Getting a GitHub token:** `Settings → Developer settings → Personal access tokens → Tokens (classic)`. Enable `repo` and `write:discussion`.

**Getting an OpenRouter key:** Sign up at [openrouter.ai](https://openrouter.ai), go to Keys, create a free key. The bot only uses `:free` tier models by default — it won't cost you anything.

---

## Usage

```bash
# Full session: finds discussions, generates answers, asks before posting
python galaxy_brain_bot.py

# Run tests first to verify your setup
python test_bot.py

# Test mode: generate answers without posting anything
python galaxy_brain_bot.py --test

# Check if any previously posted answers were accepted
python galaxy_brain_bot.py --check

# Show your accumulated stats
python galaxy_brain_bot.py --stats

# Show model performance from this session
python galaxy_brain_bot.py --models

# Override topics and star threshold for this session only
python galaxy_brain_bot.py --topics rust,go,cli --min-stars 50

# Clear in-memory cache at startup
python galaxy_brain_bot.py --cache-clear
```

---

## Session flow — what actually happens when you run it

```
┌──────────────────────────────────────────────────────────────────────┐
│                    FULL SESSION FLOWCHART                            │
└──────────────────────────────────────────────────────────────────────┘

   python galaxy_brain_bot.py
              │
              ▼
   ┌──────────────────────┐
   │  Load .env config    │
   │  Init all components │
   │  (cache, breakers,   │
   │   key/model managers)│
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  Discovery phase     │◀── DISCOVERY_TOPICS, MIN_STARS, RECENT_HOURS
   │  GraphQL → find repos│
   │  with open Discussions│
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  Filter repos        │◀── CoC check, star threshold, dedup
   │  Skip already-done   │
   │  Skip CoC-restricted │
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  For each discussion │
   │  that passes filters:│
   └──────────┬───────────┘
              │
        ┌─────┴──────┐
        ▼            ▼
  fetch body    fetch existing
  + images      comments
  + linked URLs
        │            │
        └─────┬──────┘
              │
              ▼
   ┌──────────────────────┐
   │  Build prompt        │
   │  (body + context     │
   │   + existing replies)│
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  Send to OpenRouter  │◀── tries model 1 → 2 → 3 ... (rotation)
   │  Get answer back     │
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  Length check        │
   │  (MIN_CHARS/MAX_CHARS)│
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  Show answer to you  │
   │  → confirm (y/n)?    │◀── AUTO_APPROVE_ANSWERS=false
   └──────────┬───────────┘
              │
         yes  │  no
     ┌────────┴────────┐
     ▼                 ▼
  Post to          Skip it,
  GitHub           log it
     │
     ▼
  Save to
  stats.json
     │
     ▼
  Fire webhooks
  (Discord/Slack)
              │
              ▼
   ┌──────────────────────┐
   │  Session summary     │
   │  (model performance, │
   │   badge progress,    │
   │   answers posted)    │
   └──────────────────────┘
```

---

## Free models available (via OpenRouter)

All of these are `:free` tier — no billing required.

```
┌─────────────────────────────────────────────┬──────────────┬────────┐
│ Model                                       │ Vision       │ Size   │
├─────────────────────────────────────────────┼──────────────┼────────┤
│ qwen/qwen3.6-plus:free                      │ ✓            │ Large  │
│ stepfun/step-3.5-flash:free                 │ ✓            │ Large  │
│ nvidia/nemotron-3-super-120b-a12b:free      │              │ 120B   │
│ arcee-ai/trinity-large-preview:free         │              │ Large  │
│ z-ai/glm-4.5-air:free                       │              │ Medium │
│ nvidia/nemotron-3-nano-30b-a3b:free         │              │ 30B    │
│ minimax/minimax-m2.5:free                   │ ✓            │ Large  │
│ openai/gpt-oss-120b:free                    │              │ 120B   │
│ meta-llama/llama-3.3-70b-instruct:free      │              │ 70B    │
│ google/gemma-3-27b-it:free                  │ ✓            │ 27B    │
│ + 8 more in rotation                        │              │        │
└─────────────────────────────────────────────┴──────────────┴────────┘
```

Override with `OPENROUTER_MODELS=model1,model2` in your `.env` if you want to pin specific ones.

---

## File structure

```
Galaxy-Brain-Automation/
├── galaxy_brain_bot.py        # Main bot — all logic lives here
├── galaxy_brain_stats.json    # Persisted answer history (auto-created)
├── galaxy_brain.log           # Session logs
├── test_bot.py                # Full test suite — run this first
├── test_connection.py         # Quick credential check
├── requirements.txt
├── .env                       # Your credentials (never commit this)
└── .env.template              # Example config
```

---

## Internal components

Each component is isolated enough that you can pull it into other projects.

| Component | What it does |
|---|---|
| `ShutdownHandler` | Catches SIGINT/SIGTERM, sets a flag. All loops check `shutdown.requested` before each iteration — Ctrl+C finishes cleanly instead of corrupting the stats file. |
| `InMemoryCache` | Simple dict with timestamps. TTL defaults to 300 seconds. Prevents re-fetching the same GraphQL queries within a session. |
| `CircuitBreaker` | Opens after N consecutive failures, blocks requests for a timeout, then half-opens to let one probe through. Resets on success. |
| `KeyManager` | Holds multiple OpenRouter keys, rotates to the next when one hits rate limits, tracks per-key failure counts. |
| `ModelTracker` | Records successes, failures, and latency per model. Generates the session summary table. |

---

## Running tests

```bash
# Full test suite (run this before anything else)
python test_bot.py

# Quick credential check only
python test_connection.py

# Quick end-to-end (finds repos, generates answers, doesn't post)
python test_bot.py --quick
```

The test suite checks: imports, `.env` config, GitHub API auth, OpenRouter API auth (tries each key against multiple models), GraphQL access, and stats file I/O. If it passes clean, the bot is ready to run.

---

## Ethical use

A few things worth saying plainly, not as legal cover but because they matter:

1. **Don't spam.** `MAX_ANSWERS_PER_SESSION` exists for a reason. Fifty low-quality posts in an hour will get your account flagged, and it makes the Discussion threads worse for everyone.
2. **Read the answers before posting.** `AUTO_APPROVE_ANSWERS=false` is the default. Keep it that way until you've seen enough output to trust the model on that type of question.
3. **Check the repo's code of conduct.** Some repos explicitly prohibit automated responses. The bot tries to detect this, but it's not foolproof.
4. **Don't post where you'd be unwanted.** The goal is to help people who are stuck, not to farm acceptances.

The bot was built to understand how automated discussion participation works. If you use it to post garbage at scale, that's on you.

---

## Why this codebase is worth reading

Most bot tutorials show you how to hit an API and print the response. This one actually handles failure.

The circuit breaker is a real circuit breaker, not a `try/except` with a sleep. The rate limiting reads actual server headers instead of guessing. The key rotation is stateful and tracks per-key health. The shutdown handler means Ctrl+C doesn't leave a corrupted JSON file.

These aren't exotic patterns. They're the basics every production bot needs, and they're all in one readable file.

---

## Contributing

Open an issue or PR. The most useful contributions right now are better prompt templates, more model configs, or a smarter discussion-quality filter that skips questions the bot is unlikely to answer well. PRs that just add more badges to this README will be closed immediately.

---

## License

MIT. Do what you want with the code. If it breaks something, that's between you and your GitHub account.

---

<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d1117,50:0a3d62,100:0d1117&height=140&section=footer&animation=fadeIn&text=Built%20for%20learning.%20Use%20with%20your%20brain%2C%20not%20instead%20of%20it.&fontSize=16&fontColor=8b949e&fontAlignY=55" width="100%"/>

<br/>

<img src="https://img.shields.io/badge/Made%20with-Python-3776ab?style=flat-square&logo=python&logoColor=white"/>
<img src="https://img.shields.io/badge/Powered%20by-OpenRouter-7c3aed?style=flat-square"/>
<img src="https://img.shields.io/badge/GitHub-GraphQL%20API%20v4-0969da?style=flat-square&logo=github"/>
<img src="https://img.shields.io/badge/Maintained%20by-itxashancode-58a6ff?style=flat-square"/>

<br/><br/>

**If this helped you learn something, drop a ⭐**

</div>
