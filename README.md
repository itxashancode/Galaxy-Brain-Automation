<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d1117,50:161b22,100:0d1117&height=220&section=header&text=%20Galaxy%20Brain%20Bot&fontSize=52&fontColor=58a6ff&animation=fadeIn&fontAlignY=40&desc=GitHub%20Discussions%20Automation%20%7C%20Research%20%26%20Education&descAlignY=60&descColor=8b949e" width="100%"/>

<br/>

<img src="https://img.shields.io/badge/Python-3.8%2B-3776ab?style=for-the-badge&logo=python&logoColor=white"/>
<img src="https://img.shields.io/badge/License-MIT-22863a?style=for-the-badge&logo=opensourceinitiative&logoColor=white"/>
<img src="https://img.shields.io/badge/Purpose-Educational%20Only-e3b341?style=for-the-badge&logo=bookstack&logoColor=white"/>
<img src="https://img.shields.io/badge/OpenRouter-18%2B%20Free%20Models-7c3aed?style=for-the-badge&logo=openai&logoColor=white"/>

<br/><br/>

<img src="https://img.shields.io/badge/GitHub%20GraphQL-API%20v4-0969da?style=for-the-badge&logo=github&logoColor=white"/>
<img src="https://img.shields.io/badge/Circuit%20Breakers-Resilient%20Design-16a34a?style=for-the-badge&logo=electrical&logoColor=white"/>
<img src="https://img.shields.io/badge/Adaptive%20Rate%20Limiting-Smart%20Backoff-dc2626?style=for-the-badge&logo=speedtest&logoColor=white"/>
<img src="https://img.shields.io/badge/Multi--Model-AI%20Rotation-0ea5e9?style=for-the-badge&logo=anthropic&logoColor=white"/>

<br/><br/>

<img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=600&size=18&duration=2800&pause=900&color=58A6FF&center=true&vCenter=true&width=620&lines=Auto-discovers+GitHub+Discussion+threads;Rotates+18%2B+free+AI+models+via+OpenRouter;Circuit+breakers+%2B+adaptive+rate+limiting;Badge+progress+tracker+%2B+Discord%2FSlack+webhooks;Educational+research+tool+—+use+responsibly" alt="Typing SVG"/>

</div>

---

> **⚠️ For educational and research purposes only.**
> This project demonstrates GitHub GraphQL API automation, multi-model LLM orchestration, and resilient HTTP client design patterns. It was built to study how bots interact with discussion platforms and how rate-limiting, circuit breakers, and model rotation work in practice. Running it against real GitHub repositories without understanding the implications could get your account flagged or banned. Read GitHub's [Acceptable Use Policy](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) before you do anything.

---

## What this is

Galaxy Brain Bot searches public GitHub repositories for open Discussion threads, generates answers using free LLMs via OpenRouter, and optionally posts them. It tracks which answers get marked as "Accepted" so you can watch your [Galaxy Brain badge](https://docs.github.com/en/discussions/guides/finding-your-discussions) progress.

The codebase is more interesting than it sounds. It has:

- A proper circuit breaker that stops hammering a failing endpoint after N consecutive errors
- Adaptive rate limiting that reads `Retry-After` headers and backs off intelligently
- A round-robin API key manager that rotates across multiple OpenRouter keys
- TTL-based in-memory caching to avoid redundant GraphQL fetches within a session
- Multi-modal support — it can pull image context from discussion posts if the model supports vision
- Link fetching that grabs external page content and feeds it into the prompt
- Discord and Slack webhook notifications for accepted answers

None of these are exotic. They're the patterns any production bot needs. The code is readable and each component is isolated enough that you can pull pieces out for other projects.

---

## Features

### Core
- **Auto-discovery** — finds repos by topic tags, star count, and activity recency. No hardcoded lists.
- **Smart deduplication** — persists answered discussion IDs to `galaxy_brain_stats.json` so it never double-posts across sessions
- **CoC-aware filtering** — skips repos with codes of conduct that restrict automated participation
- **Comment-aware** — reads existing comments before answering to avoid redundant replies

### Reliability
- **Circuit breakers** — per-endpoint, configurable threshold and timeout
- **Adaptive backoff** — respects `Retry-After`, `X-RateLimit-Reset`, and falls back to exponential delays
- **Key rotation** — cycles through multiple OpenRouter API keys when one hits rate limits
- **Model rotation** — tries 18+ free models in order and skips broken ones automatically

### AI Integration
- **18+ free models** via OpenRouter — Qwen, Llama, Gemma, Nemotron, GPT-OSS, and more
- **Vision support** — detects if a model supports images and includes them when relevant
- **Link fetching** — pulls external URLs from discussion bodies for richer context
- **Configurable answer length** — min/max character limits prevent low-effort one-liners

### Tracking
- **Badge progress** — shows your accepted answer count and which tier you're on
- **Stats by org** — breaks down your answers by repository owner
- **Session model performance** — tracks which AI models succeed vs. fail and their latency
- **Webhook notifications** — Discord/Slack alerts on new acceptances and session summaries

---

## Installation

**Requirements:** Python 3.8+, a GitHub account, a free [OpenRouter](https://openrouter.ai) account.

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/galaxy-brain-bot.git
cd galaxy-brain-bot

# Install dependencies
pip install -r requirements.txt

# Copy and fill in your credentials
cp .env.template .env
nano .env
```

**Requirements file:**
```
requests>=2.31.0
rich>=13.7.0
python-dotenv>=1.0.0
```

---

## Configuration

Create a `.env` file in the project root:

```env
# GitHub — needs read:discussion and write:discussion scopes
GITHUB_TOKEN=ghp_your_token_here
GITHUB_USERNAME=your_github_username

# OpenRouter — comma-separated if you have multiple keys
OPENROUTER_KEYS=sk-or-v1-your_key_here

# Optional: override which models to use (defaults to the 18-model list in bot)
# OPENROUTER_MODELS=qwen/qwen3.6-plus:free,meta-llama/llama-3.3-70b-instruct:free

# Webhook notifications (optional)
DISCORD_WEBHOOK_URL=
SLACK_WEBHOOK_URL=

# Bot behavior
MIN_REPO_STARS=10
MAX_ANSWERS_PER_SESSION=5
DELAY_BETWEEN_ANSWERS=5
AUTO_APPROVE_ANSWERS=false

# Discovery
DISCOVERY_TOPICS=python,javascript,open-source,programming
DISCOVERY_MIN_STARS=5
DISCOVERY_MAX_REPOS=50

# Answer length
ANSWER_MIN_CHARS=120
ANSWER_MAX_CHARS=900

# Performance tuning
CACHE_TTL_SECONDS=300
CIRCUIT_BREAKER_THRESHOLD=5
CIRCUIT_BREAKER_TIMEOUT=120
RECENT_HOURS=24
```

**Getting your GitHub token:** Go to `Settings → Developer settings → Personal access tokens → Tokens (classic)`. Enable `repo` and `write:discussion` scopes.

**Getting an OpenRouter key:** Sign up at [openrouter.ai](https://openrouter.ai), go to Keys, create a free key. The bot only uses `:free` tier models by default, so it won't charge you anything.

---

## Usage

```bash
# Run a full session (finds discussions, generates answers, asks before posting)
python galaxy_brain_bot.py

# Run tests first to verify your setup
python test_bot.py

# Skip posting, just see what answers it would generate
python galaxy_brain_bot.py --test

# Check if any previously posted answers were accepted
python galaxy_brain_bot.py --check

# Show your accumulated stats
python galaxy_brain_bot.py --stats

# Show which AI models performed well this session
python galaxy_brain_bot.py --models

# Override topics and star threshold for this session
python galaxy_brain_bot.py --topics rust,go,cli --min-stars 50

# Clear the in-memory cache at startup
python galaxy_brain_bot.py --cache-clear
```

---

## How the model rotation works

The bot tries models in order. If a model returns an empty response, a 429, or a 404, it logs that failure and moves to the next one. At the end of each session, `--models` shows you a table of which models succeeded, how many times they failed, and their average response latency.

Default order (all free tier):
```
qwen/qwen3.6-plus:free
stepfun/step-3.5-flash:free
nvidia/nemotron-3-super-120b-a12b:free
arcee-ai/trinity-large-preview:free
z-ai/glm-4.5-air:free
nvidia/nemotron-3-nano-30b-a3b:free
minimax/minimax-m2.5:free
openai/gpt-oss-120b:free
meta-llama/llama-3.3-70b-instruct:free
google/gemma-3-27b-it:free
... and more
```

Override with `OPENROUTER_MODELS=model1,model2` in your `.env`.

---

## File structure

```
galaxy-brain-bot/
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

## Stats and badge tracking

The bot tracks everything in `galaxy_brain_stats.json`:

```json
{
  "total_answers": 12,
  "accepted_answers": 3,
  "acceptance_rate": 0.25,
  "answers": [...],
  "answered_discussion_ids": [...]
}
```

GitHub's Galaxy Brain badge tiers (as of 2025):
- **Bronze** — 8 accepted answers
- **Silver** — 16 accepted answers
- **Gold** — 32 accepted answers

The bot shows your current tier and how far you are from the next one.

---

## Ethical use

A few things worth saying plainly:

1. **Don't spam.** The bot has `MAX_ANSWERS_PER_SESSION` for a reason. Posting 50 low-quality answers in an hour will get you flagged.
2. **Read answers before posting.** `AUTO_APPROVE_ANSWERS=false` means the bot asks you to confirm each one. Keep it that way until you trust the output quality.
3. **Check the repo's code of conduct.** Some repos explicitly prohibit automated responses. The bot tries to detect this, but it's not perfect.
4. **Don't use it on repos where you'd be unwanted.** The point is to help people, not to farm badges.

This tool was built to understand how automated discussion participation works, not to game GitHub. If you use it to post garbage at scale, that's on you.

---

## Architecture notes

For anyone who wants to understand the internals before modifying them:

**`ShutdownHandler`** catches SIGINT/SIGTERM and sets a flag. All loops check `shutdown.requested` before each iteration, so Ctrl+C finishes the current task cleanly instead of corrupting the stats file.

**`InMemoryCache`** is a simple dict with timestamps. The TTL defaults to 300 seconds. It prevents re-fetching the same GraphQL queries within a session when the bot is processing a large target list.

**`CircuitBreaker`** opens after N consecutive failures, blocks requests for a timeout period, then half-opens to let one probe through. If the probe succeeds, it resets. This prevents the bot from hammering a broken endpoint for the entire session.

**`KeyManager`** holds multiple OpenRouter keys and rotates to the next one when a key hits a rate limit. It also tracks per-key failure counts.

**`ModelTracker`** records successes, failures, and latency per model. The session summary table at the end comes from this.

---

## Running tests

```bash
# Full test suite
python test_bot.py

# Quick credential check only
python test_connection.py

# Quick end-to-end (finds repos but doesn't post)
python test_bot.py --quick
```

The test suite checks imports, `.env` configuration, GitHub API auth, OpenRouter API auth (tries each key against multiple models), GraphQL access, and stats file I/O.

---

## License

MIT — do what you want with the code, but don't blame me if something breaks.

---

## Contributing

Open an issue or PR. The most useful contributions right now would be better prompt templates, additional model configs, or a smarter discussion-quality filter so the bot skips questions it's unlikely to answer well.

---

<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d1117,50:161b22,100:0d1117&height=120&section=footer&animation=fadeIn" width="100%"/>

**Built for learning. Use with your brain, not instead of it.**

<img src="https://img.shields.io/badge/Made%20with-Python-3776ab?style=flat-square&logo=python&logoColor=white"/>
<img src="https://img.shields.io/badge/Powered%20by-OpenRouter-7c3aed?style=flat-square"/>
<img src="https://img.shields.io/badge/GitHub-GraphQL%20API%20v4-0969da?style=flat-square&logo=github"/>

</div>
