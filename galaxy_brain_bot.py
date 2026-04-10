import os
import sys
import time
import json
import signal
import logging
import argparse
import re
import hashlib
import threading
from collections import defaultdict, deque, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
import base64
import mimetypes
import urllib.parse
import requests
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm
from rich.panel import Panel
from dotenv import load_dotenv

load_dotenv()
console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

LOG_FILE = os.getenv("LOG_FILE", "galaxy_brain.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("galaxy_brain")

# ─────────────────────────────────────────────────────────────────────────────
# Constants / env-overridable settings
# ─────────────────────────────────────────────────────────────────────────────

MAX_COMMENT_CHARS = 65_536
BODY_TRUNCATE_CHARS = int(os.getenv("BODY_TRUNCATE_CHARS", "2500"))
REPO_COOLDOWN_MINUTES = int(os.getenv("REPO_COOLDOWN_MINUTES", "60"))
STALENESS_DAYS = int(os.getenv("STALENESS_DAYS", "180"))  # skip questions older than this

def _require_env_int(key: str) -> int:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is missing or empty in .env")
    try:
        return int(val)
    except ValueError:
        raise EnvironmentError(f"Env var '{key}' must be an integer, got: {val!r}")

def _require_env_float(key: str) -> float:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is missing or empty in .env")
    try:
        return float(val)
    except ValueError:
        raise EnvironmentError(f"Env var '{key}' must be a number, got: {val!r}")

def _require_env_bool(key: str) -> bool:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is missing or empty in .env")
    return val.lower() == "true"

def _require_env_str(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is missing or empty in .env")
    return val

try:
    ANSWER_MIN_CHARS               = _require_env_int("ANSWER_MIN_CHARS")
    ANSWER_MAX_CHARS               = _require_env_int("ANSWER_MAX_CHARS")
    RATE_LIMIT_RETRY_AFTER_DEFAULT = _require_env_int("RATE_LIMIT_RETRY_AFTER")
    RATE_LIMIT_ROTATE_AFTER        = _require_env_int("RATE_LIMIT_ROTATE_AFTER")
    PAGE_DELAY                     = _require_env_float("PAGE_FETCH_DELAY")
    MODEL_ATTEMPT_DELAY            = _require_env_float("MODEL_ATTEMPT_DELAY")
    RECENT_HOURS                   = _require_env_int("RECENT_HOURS")
    CACHE_TTL_SECONDS              = int(os.getenv("CACHE_TTL_SECONDS", "300"))
    CIRCUIT_BREAKER_THRESHOLD      = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5"))
    CIRCUIT_BREAKER_TIMEOUT        = int(os.getenv("CIRCUIT_BREAKER_TIMEOUT", "120"))
    HEALTH_CHECK_PORT              = int(os.getenv("HEALTH_CHECK_PORT", "0"))

    # Multi-modal / link fetching — required from .env
    ENABLE_IMAGE_ANALYSIS = _require_env_bool("ENABLE_IMAGE_ANALYSIS")
    ENABLE_LINK_FETCH     = _require_env_bool("ENABLE_LINK_FETCH")
    LINK_FETCH_TIMEOUT    = _require_env_int("LINK_FETCH_TIMEOUT")
    LINK_FETCH_MAX_CHARS  = _require_env_int("LINK_FETCH_MAX_CHARS")
    IMAGE_MAX_BYTES       = _require_env_int("IMAGE_MAX_BYTES")
    MAX_IMAGES_PER_POST   = _require_env_int("MAX_IMAGES_PER_POST")
    MAX_LINKS_PER_POST    = _require_env_int("MAX_LINKS_PER_POST")

    # Auto-discovery settings — required from .env
    _discovery_raw = _require_env_str("DISCOVERY_TOPICS")
    DISCOVERY_TOPICS    = [t.strip() for t in _discovery_raw.split(",") if t.strip()]
    DISCOVERY_MIN_STARS = _require_env_int("DISCOVERY_MIN_STARS")
    DISCOVERY_MAX_REPOS = _require_env_int("DISCOVERY_MAX_REPOS")

except EnvironmentError as _env_err:
    print(f"[ERROR] Configuration error: {_env_err}")
    print("Please ensure all required variables are set in your .env file.")
    sys.exit(1)

# Models that support vision (checked against model name substring)
_VISION_MODEL_HINTS = [
    "gpt-4o", "gpt-4-vision", "claude", "gemini", "llava", "pixtral",
    "qwen-vl", "qwen2-vl", "internvl", "phi-3-vision", "mistral-pixtral",
]

_DEFAULT_MODELS = [
    "qwen/qwen3.6-plus:free",
    "stepfun/step-3.5-flash:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "arcee-ai/trinity-large-preview:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "minimax/minimax-m2.5:free",
    "arcee-ai/trinity-mini:free",
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "openai/gpt-oss-20b:free",
    "qwen/qwen3-coder:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "google/gemma-3-12b-it:free",
    "google/gemma-3-4b-it:free",
]


def _load_models() -> List[str]:
    env_val = os.getenv("OPENROUTER_MODELS", "").strip()
    if env_val:
        models = [m.strip() for m in env_val.split(",") if m.strip()]
        if models:
            return models
    return _DEFAULT_MODELS


MODELS = _load_models()


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown handler
# ─────────────────────────────────────────────────────────────────────────────

class ShutdownHandler:
    """Catch SIGINT/SIGTERM and set a flag so loops can exit cleanly."""
    def __init__(self):
        self._shutdown = False
        signal.signal(signal.SIGINT,  self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, *_):
        console.print("\n[yellow]Shutdown signal received — finishing current task...[/yellow]")
        logger.info("Shutdown signal received")
        self._shutdown = True

    @property
    def requested(self) -> bool:
        return self._shutdown


shutdown = ShutdownHandler()


# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ─────────────────────────────────────────────────────────────────────────────

class InMemoryCache:
    """Simple TTL cache — avoids redundant HTTP calls within a session."""
    def __init__(self, ttl: int = CACHE_TTL_SECONDS):
        self._store: Dict[str, Tuple[float, object]] = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry:
                if (time.time() - entry[0]) < self._ttl:
                    return entry[1]
                del self._store[key]
            return None

    def set(self, key: str, value):
        with self._lock:
            self._store[key] = (time.time(), value)

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

    def stats(self) -> Dict:
        with self._lock:
            alive = sum(1 for ts, _ in self._store.values() if (time.time() - ts) < self._ttl)
            return {"total_keys": len(self._store), "live_keys": alive}


cache = InMemoryCache()


# ─────────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ─────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    After CIRCUIT_BREAKER_THRESHOLD consecutive failures the circuit opens
    and blocks calls for CIRCUIT_BREAKER_TIMEOUT seconds, then half-opens
    for one probe request.
    """
    CLOSED = "closed"
    OPEN   = "open"
    HALF   = "half_open"

    def __init__(self, name: str, threshold: int = CIRCUIT_BREAKER_THRESHOLD,
                 timeout: int = CIRCUIT_BREAKER_TIMEOUT):
        self.name      = name
        self.threshold = threshold
        self.timeout   = timeout
        self.state     = self.CLOSED
        self.failures  = 0
        self.opened_at: Optional[float] = None
        self._lock     = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.state == self.CLOSED:
                return True
            if self.state == self.OPEN:
                if time.time() - self.opened_at > self.timeout:
                    self.state = self.HALF
                    logger.info(f"CircuitBreaker [{self.name}] -> half-open (probe)")
                    return True
                return False
            return True  # HALF_OPEN — allow the probe

    def record_success(self):
        with self._lock:
            if self.state != self.CLOSED:
                logger.info(f"CircuitBreaker [{self.name}] -> closed")
            self.state     = self.CLOSED
            self.failures  = 0
            self.opened_at = None

    def record_failure(self):
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                if self.state != self.OPEN:
                    logger.warning(f"CircuitBreaker [{self.name}] -> open after {self.failures} failures")
                    console.print(f"[red]Circuit breaker OPEN for {self.name} — pausing requests[/red]")
                self.state     = self.OPEN
                self.opened_at = time.time()

    def status(self) -> str:
        with self._lock:
            return self.state


_cb_github     = CircuitBreaker("github_graphql")
_cb_openrouter = CircuitBreaker("openrouter")


# ─────────────────────────────────────────────────────────────────────────────
# Smart rate limiter with adaptive backoff
# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveRateLimiter:
    """Sliding-window rate limiter with exponential backoff on 429s."""
    def __init__(self, name: str, max_calls: int = 20, window_size: int = 60):
        self.name        = name
        self.max_calls   = max_calls
        self.window_size = window_size
        self._calls: deque = deque()
        self._backoff    = 1
        self._lock       = threading.Lock()

    def wait_if_needed(self):
        with self._lock:
            now = time.time()
            while self._calls and now - self._calls[0] > self.window_size:
                self._calls.popleft()
            sleep_for = 0.0
            if len(self._calls) >= self.max_calls:
                oldest    = self._calls[0]
                sleep_for = self.window_size - (now - oldest) + 0.1
        if sleep_for > 0:
            logger.debug(f"RateLimiter [{self.name}] sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
        with self._lock:
            self._calls.append(time.time())

    def backoff(self):
        """Call after a 429 — doubles sleep up to 120s."""
        sleep = min(self._backoff * 2, 120)
        logger.warning(f"RateLimiter [{self.name}] adaptive backoff {sleep}s")
        time.sleep(sleep)
        self._backoff = min(self._backoff * 2, 64)

    def reset_backoff(self):
        self._backoff = 1


_rl_github     = AdaptiveRateLimiter("github",     max_calls=15, window_size=60)
_rl_openrouter = AdaptiveRateLimiter("openrouter", max_calls=20, window_size=60)


# ─────────────────────────────────────────────────────────────────────────────
# Request deduplication / idempotency
# ─────────────────────────────────────────────────────────────────────────────

class RequestDeduplicator:
    """Prevents identical API calls (e.g. posting the same answer twice)."""
    _MAX_SIZE = 10_000

    def __init__(self):
        self._seen: "OrderedDict[str, None]" = OrderedDict()

    def fingerprint(self, *parts) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def is_duplicate(self, *parts) -> bool:
        fp = self.fingerprint(*parts)
        if fp in self._seen:
            return True
        self._seen[fp] = None
        while len(self._seen) > self._MAX_SIZE:
            self._seen.popitem(last=False)
        return False


deduplicator = RequestDeduplicator()


# ─────────────────────────────────────────────────────────────────────────────
# Per-repo cooldown tracker
# ─────────────────────────────────────────────────────────────────────────────

class RepoCooldownTracker:
    """Prevents posting to the same repo too frequently within a session."""

    def __init__(self, cooldown_minutes: int = REPO_COOLDOWN_MINUTES):
        self._last_posted: Dict[str, float] = {}
        self._cooldown = cooldown_minutes * 60
        self._lock = threading.Lock()

    def is_cooled_down(self, repo_key: str) -> bool:
        with self._lock:
            last = self._last_posted.get(repo_key)
            if last is None:
                return True
            return (time.time() - last) >= self._cooldown

    def record_post(self, repo_key: str):
        with self._lock:
            self._last_posted[repo_key] = time.time()

    def seconds_remaining(self, repo_key: str) -> int:
        with self._lock:
            last = self._last_posted.get(repo_key)
            if last is None:
                return 0
            remaining = self._cooldown - (time.time() - last)
            return max(0, int(remaining))


repo_cooldown = RepoCooldownTracker()


# ─────────────────────────────────────────────────────────────────────────────
# Answer uniqueness: fingerprint past answers to avoid repetitive posts
# ─────────────────────────────────────────────────────────────────────────────

class AnswerUniquenessChecker:
    """
    Stores 6-gram shingle fingerprints of past answers this session.
    Rejects a new answer if it overlaps too much with any prior answer.
    """
    _MAX_SHINGLES = 50_000
    _OVERLAP_THRESHOLD = 0.35  # reject if Jaccard similarity >= this

    def __init__(self):
        self._shingle_sets: List[Set[str]] = []

    @staticmethod
    def _shingles(text: str, k: int = 6) -> Set[str]:
        words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
        return {" ".join(words[i:i+k]) for i in range(len(words) - k + 1)} if len(words) >= k else set(words)

    def is_unique(self, answer: str) -> Tuple[bool, float]:
        """Returns (is_unique, max_similarity_seen)."""
        new_sh = self._shingles(answer)
        if not new_sh:
            return True, 0.0
        max_sim = 0.0
        for past_sh in self._shingle_sets:
            if not past_sh:
                continue
            intersection = len(new_sh & past_sh)
            union = len(new_sh | past_sh)
            sim = intersection / union if union else 0.0
            if sim > max_sim:
                max_sim = sim
            if sim >= self._OVERLAP_THRESHOLD:
                return False, sim
        return True, max_sim

    def register(self, answer: str):
        sh = self._shingles(answer)
        self._shingle_sets.append(sh)
        # Trim oldest entries if we're accumulating too many shingles
        total = sum(len(s) for s in self._shingle_sets)
        while total > self._MAX_SHINGLES and self._shingle_sets:
            removed = self._shingle_sets.pop(0)
            total -= len(removed)


answer_uniqueness = AnswerUniquenessChecker()


# ─────────────────────────────────────────────────────────────────────────────
# Smart model selection — tracks per-model performance
# ─────────────────────────────────────────────────────────────────────────────

class ModelPerformanceTracker:
    """
    Tracks success rate and latency per model.
    sorted_models() returns the list ordered best-first so we try proven
    models before falling back to untested ones.
    """
    def __init__(self, models: List[str]):
        self._stats: Dict[str, Dict] = {
            m: {"successes": 0, "failures": 0, "total_latency": 0.0, "empty_responses": 0}
            for m in models
        }

    def record(self, model: str, success: bool, latency: float = 0.0, empty: bool = False):
        s = self._stats.setdefault(model, {"successes": 0, "failures": 0, "total_latency": 0.0, "empty_responses": 0})
        if success:
            s["successes"] += 1
            s["total_latency"] += latency
        elif empty:
            s["empty_responses"] += 1
        else:
            s["failures"] += 1

    def score(self, model: str) -> float:
        s = self._stats.get(model, {})
        wins   = s.get("successes", 0)
        losses = s.get("failures", 0) + s.get("empty_responses", 0) * 0.5
        calls  = wins + losses
        if calls == 0:
            return 0.5  # neutral prior for untested models
        win_rate = wins / calls
        avg_lat  = (s["total_latency"] / wins) if wins else 10.0
        return win_rate - (avg_lat / 200.0)

    def sorted_models(self, candidates: List[str]) -> List[str]:
        return sorted(candidates, key=lambda m: self.score(m), reverse=True)

    def summary(self) -> List[Tuple[str, int, int, float]]:
        rows = []
        for m, s in self._stats.items():
            if s["successes"] + s["failures"] > 0:
                avg = s["total_latency"] / max(s["successes"], 1)
                rows.append((m, s["successes"], s["failures"], avg))
        return sorted(rows, key=lambda r: r[1], reverse=True)


model_tracker = ModelPerformanceTracker(MODELS)


# ─────────────────────────────────────────────────────────────────────────────
# Quality gate
# ─────────────────────────────────────────────────────────────────────────────

_ANNOUNCEMENT_PATTERNS = re.compile(
    r"(we'?re (happy|excited|pleased|proud) to (announce|introduce|release|share)|"
    r"releasing v\d|"
    r"introducing\s+v?\d|"
    r"changelog|"
    r"release notes|"
    r"new release|"
    r"announcing\s+|"
    r"version \d+\.\d+)",
    re.IGNORECASE,
)

_ERROR_PATTERNS = re.compile(
    r"(traceback|error:|exception:|stack trace|fatal:|stderr|"
    r"undefined|null pointer|segfault|exit code|failed to|"
    r"cannot|can't|won't start|doesn't work|not working|broken)",
    re.IGNORECASE,
)


def is_answerable(discussion: Dict) -> Tuple[bool, str]:
    title = (discussion.get("title") or "").strip()
    body  = (discussion.get("body")  or "").strip()
    if len(title) < 10:
        return False, "title too short"
    if len(body) < 20:
        return False, "body too short"
    if discussion.get("closed"):
        return False, "closed"

    # Staleness check — skip questions that have gone cold
    created_at = discussion.get("createdAt") or discussion.get("updatedAt")
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created_dt).days
            if age_days > STALENESS_DAYS:
                return False, f"stale ({age_days}d old, limit {STALENESS_DAYS}d)"
        except Exception:
            pass

    combined = title + " " + body

    if _ANNOUNCEMENT_PATTERNS.search(combined):
        return False, "announcement/release post"

    score = 0
    if "?" in combined:
        score += 2
    if _ERROR_PATTERNS.search(combined):
        score += 2
    if "```" in body or "`" in body:
        score += 1
    if len(body) < 500 and "?" in combined:
        score += 1

    if score == 0:
        return False, "no clear question signal"

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Webhook notifier
# ─────────────────────────────────────────────────────────────────────────────

class WebhookNotifier:
    def __init__(self):
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.slack_webhook   = os.getenv("SLACK_WEBHOOK_URL",   "").strip()
        self.discord_webhook = self.discord_webhook if "your_webhook" not in self.discord_webhook else ""
        self.slack_webhook   = (
            self.slack_webhook
            if "hooks.slack.com" in self.slack_webhook and "your" not in self.slack_webhook
            else ""
        )
        self.enabled = bool(self.discord_webhook or self.slack_webhook)
        if self.enabled:
            console.print("[green]Webhooks configured[/green]")

    def send_answer_notification(self, title, repo, answer_preview, url, answer_length):
        if self.discord_webhook:
            self._post_discord({"embeds": [{
                "title": "New Answer Posted",
                "description": f"**Question:** {title}\n**Repository:** {repo}",
                "color": 0x00FF00,
                "fields": [
                    {"name": "Preview", "value": answer_preview[:300] + "...", "inline": False},
                    {"name": "Length",  "value": f"{answer_length} chars",     "inline": True},
                    {"name": "Link",    "value": f"[View]({url})",              "inline": True},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Galaxy Brain Bot v6"},
            }]})
        if self.slack_webhook:
            self._post_slack({"blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "New Answer Posted"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Question:*\n{title}"},
                    {"type": "mrkdwn", "text": f"*Repository:*\n{repo}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Preview:*\n{answer_preview[:300]}..."}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{answer_length} chars - <{url}|View>"}]},
            ]})

    def send_acceptance_notification(self, title, repo, total_accepted, badge_tier):
        if self.discord_webhook:
            self._post_discord({"embeds": [{
                "title": "Answer Accepted", "color": 0xFFCC00,
                "fields": [
                    {"name": "Question",       "value": title,               "inline": False},
                    {"name": "Repository",     "value": repo,                "inline": True},
                    {"name": "Total Accepted", "value": str(total_accepted), "inline": True},
                    {"name": "Badge Progress", "value": badge_tier,          "inline": False},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Galaxy Brain Bot v6"},
            }]})
        if self.slack_webhook:
            self._post_slack({"blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "Answer Accepted"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Question:*\n{title}"},
                    {"type": "mrkdwn", "text": f"*Repository:*\n{repo}"},
                    {"type": "mrkdwn", "text": f"*Total Accepted:*\n{total_accepted}"},
                    {"type": "mrkdwn", "text": f"*Badge Tier:*\n{badge_tier}"},
                ]},
            ]})

    def send_batch_summary(self, answered_count, total_answers, acceptance_rate, badge_tier):
        if self.discord_webhook:
            self._post_discord({"embeds": [{
                "title": "Session Summary", "color": 0x0099FF,
                "fields": [
                    {"name": "This Session",    "value": str(answered_count),       "inline": True},
                    {"name": "Lifetime Total",  "value": str(total_answers),        "inline": True},
                    {"name": "Acceptance Rate", "value": f"{acceptance_rate:.1f}%", "inline": True},
                    {"name": "Badge Tier",      "value": badge_tier,                "inline": False},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Galaxy Brain Bot v6"},
            }]})
        if self.slack_webhook:
            self._post_slack({"blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "Session Summary"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Posted:*\n{answered_count}"},
                    {"type": "mrkdwn", "text": f"*Lifetime:*\n{total_answers}"},
                    {"type": "mrkdwn", "text": f"*Acceptance Rate:*\n{acceptance_rate:.1f}%"},
                    {"type": "mrkdwn", "text": f"*Badge Tier:*\n{badge_tier}"},
                ]},
            ]})

    def _post_discord(self, payload):
        try:
            r = requests.post(self.discord_webhook, json=payload, timeout=10)
            if r.status_code not in (200, 204):
                logger.warning(f"Discord webhook error: {r.status_code}")
        except Exception as e:
            logger.warning(f"Discord error: {e}")

    def _post_slack(self, payload):
        try:
            r = requests.post(self.slack_webhook, json=payload, timeout=10)
            if r.status_code not in (200, 204):
                logger.warning(f"Slack webhook error: {r.status_code}")
        except Exception as e:
            logger.warning(f"Slack error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# GitHub GraphQL client (cache + circuit breaker + rate limiter)
# ─────────────────────────────────────────────────────────────────────────────

class GitHubDiscussionsAPI:

    def __init__(self, token: str, username: str):
        self.token    = token
        self.username = username
        self.api_url  = "https://api.github.com/graphql"
        self.rest_url = "https://api.github.com"
        self.headers  = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }
        self.rest_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._category_cache: Dict[Tuple[str, str], Optional[str]] = {}
        self._coc_cache: Dict[str, Optional[str]] = {}

    def _gql(self, query: str, variables: Dict = None, cache_key: str = None) -> Optional[Dict]:
        if cache_key:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        if not _cb_github.allow():
            logger.warning("GitHub circuit breaker OPEN — skipping request")
            return None

        _rl_github.wait_if_needed()
        last_exc = None
        for attempt in range(3):
            try:
                r = requests.post(
                    self.api_url,
                    headers=self.headers,
                    json={"query": query, "variables": variables or {}},
                    timeout=30,
                )
                if r.status_code == 200:
                    result = r.json()
                    if "errors" in result:
                        console.print(f"[red]GraphQL errors: {result['errors']}[/red]")
                        _cb_github.record_failure()
                        return None
                    data = result.get("data")
                    _cb_github.record_success()
                    if cache_key and data:
                        cache.set(cache_key, data)
                    return data
                elif r.status_code == 429:
                    _rl_github.backoff()
                    _cb_github.record_failure()
                    return None
                else:
                    console.print(f"[red]HTTP {r.status_code}: {r.text[:200]}[/red]")
                    _cb_github.record_failure()
                    return None
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                wait = 2 ** attempt
                logger.warning(f"GitHub network error (attempt {attempt+1}/3): {e} — retrying in {wait}s")
                time.sleep(wait)
            except Exception as e:
                console.print(f"[red]Request error: {e}[/red]")
                _cb_github.record_failure()
                return None
        console.print(f"[red]GitHub network error after 3 attempts: {last_exc}[/red]")
        _cb_github.record_failure()
        return None

    # ── Repo Discovery ─────────────────────────────────────────────────────────

    def discover_repos_with_discussions(
        self,
        topics: List[str],
        min_stars: int = 5,
        max_repos: int = 50,
    ) -> List[Tuple[str, str]]:
        console.print(f"\n[bold cyan]🔭 Auto-discovering repos (topics: {', '.join(topics[:5])})[/bold cyan]")
        found: Dict[str, Tuple[str, str]] = {}

        for topic in topics:
            if len(found) >= max_repos or shutdown.requested:
                break
            ck = f"discover:{topic}:{min_stars}"
            cached_items = cache.get(ck)
            if cached_items:
                for k, v in cached_items:
                    found[k] = v
                continue
            query = f"topic:{topic} has:discussions is:public stars:>={min_stars}"
            try:
                _rl_github.wait_if_needed()
                r = requests.get(
                    f"{self.rest_url}/search/repositories",
                    headers=self.rest_headers,
                    params={"q": query, "sort": "updated", "order": "desc", "per_page": 20},
                    timeout=20,
                )
                if r.status_code == 200:
                    items = r.json().get("items", [])
                    batch = []
                    for item in items:
                        key = item["full_name"]
                        if key not in found:
                            owner, repo = item["owner"]["login"], item["name"]
                            found[key] = (owner, repo)
                            batch.append((key, (owner, repo)))
                    cache.set(ck, batch)
                elif r.status_code == 403:
                    console.print("[yellow]Search rate limited, using cached results[/yellow]")
                    break
                time.sleep(0.3)
            except Exception as e:
                console.print(f"[yellow]Search error for topic {topic}: {e}[/yellow]")

        if len(found) < max_repos:
            gql_query = """
            query($q: String!) {
                search(query: $q, type: REPOSITORY, first: 25) {
                    nodes {
                        ... on Repository {
                            nameWithOwner
                            owner { login }
                            name
                            hasDiscussionsEnabled
                            stargazerCount
                        }
                    }
                }
            }
            """
            for topic in topics[:3]:
                if len(found) >= max_repos or shutdown.requested:
                    break
                data = self._gql(gql_query, {"q": f"topic:{topic} is:public stars:>={min_stars}"},
                                  cache_key=f"gql_discover:{topic}:{min_stars}")
                if data:
                    nodes = data.get("search", {}).get("nodes", [])
                    for n in nodes:
                        if n.get("hasDiscussionsEnabled"):
                            key = n.get("nameWithOwner", "")
                            if key and key not in found:
                                found[key] = (n["owner"]["login"], n["name"])
                time.sleep(0.3)

        result = list(found.values())[:max_repos]
        console.print(f"[green]Discovered {len(result)} repos with discussions[/green]")
        return result

    # ── CoC Fetcher ────────────────────────────────────────────────────────────

    def fetch_code_of_conduct(self, owner: str, repo: str) -> Optional[str]:
        cache_key = f"coc:{owner}/{repo}"
        if cache_key in self._coc_cache:
            return self._coc_cache[cache_key]
        cached = cache.get(cache_key)
        if cached is not None:
            self._coc_cache[cache_key] = cached
            return cached

        coc_text = None
        try:
            _rl_github.wait_if_needed()
            r = requests.get(
                f"{self.rest_url}/repos/{owner}/{repo}/community/code_of_conduct",
                headers={**self.rest_headers, "Accept": "application/vnd.github.scarlet-witch-preview+json"},
                timeout=10,
            )
            if r.status_code == 200:
                body = r.json().get("body", "")
                if body:
                    coc_text = body[:2000]
        except Exception:
            pass

        if not coc_text:
            for path in ["CODE_OF_CONDUCT.md", "docs/CODE_OF_CONDUCT.md", ".github/CODE_OF_CONDUCT.md", "CONDUCT.md"]:
                try:
                    _rl_github.wait_if_needed()
                    r = requests.get(
                        f"{self.rest_url}/repos/{owner}/{repo}/contents/{path}",
                        headers=self.rest_headers, timeout=10,
                    )
                    if r.status_code == 200:
                        content = r.json().get("content", "")
                        if content:
                            decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
                            coc_text = decoded[:2000]
                            break
                except Exception:
                    pass

        self._coc_cache[cache_key] = coc_text
        cache.set(cache_key, coc_text)
        if coc_text:
            console.print(f"[dim]  CoC fetched for {owner}/{repo} ({len(coc_text)} chars)[/dim]")
        return coc_text

    # ── Category resolver ──────────────────────────────────────────────────────

    def get_qa_category_id(self, owner: str, repo: str) -> Optional[str]:
        cache_key_tuple = (owner, repo)
        if cache_key_tuple in self._category_cache:
            return self._category_cache[cache_key_tuple]
        cached = cache.get(f"cat:{owner}/{repo}")
        if cached is not None:
            self._category_cache[cache_key_tuple] = cached
            return cached

        query = """
        query GetCategories($owner: String!, $name: String!) {
            repository(owner: $owner, name: $name) {
                discussionCategories(first: 25) {
                    nodes { id name isAnswerable }
                }
            }
        }
        """
        data = self._gql(query, {"owner": owner, "name": repo},
                         cache_key=f"cat_raw:{owner}/{repo}")
        if not data:
            self._category_cache[cache_key_tuple] = None
            return None

        nodes = (data.get("repository") or {}).get("discussionCategories", {}).get("nodes", [])
        qa_keywords  = ["q&a", "question", "help", "support", "q and a"]
        category_id  = None

        for node in nodes:
            if node.get("isAnswerable"):
                name_lower = node["name"].lower()
                if any(kw in name_lower for kw in qa_keywords):
                    category_id = node["id"]
                    console.print(f"  [dim]{owner}/{repo}: Q&A category '{node['name']}'[/dim]")
                    break

        if not category_id:
            for node in nodes:
                if node.get("isAnswerable"):
                    category_id = node["id"]
                    console.print(f"  [dim]{owner}/{repo}: answerable category '{node['name']}' (fallback)[/dim]")
                    break

        if not category_id:
            console.print(f"  [dim]{owner}/{repo}: no answerable categories — skipping[/dim]")

        self._category_cache[cache_key_tuple] = category_id
        cache.set(f"cat:{owner}/{repo}", category_id)
        return category_id

    # ── Discussions fetcher ────────────────────────────────────────────────────

    def get_unanswered_discussions(
        self,
        owner: str,
        repo: str,
        label: Optional[str] = None,
        max_pages: int = 3,
    ) -> List[Dict]:
        category_id = self.get_qa_category_id(owner, repo)
        if not category_id:
            return []

        query = """
        query GetDiscussions($owner: String!, $name: String!, $categoryId: ID!, $cursor: String) {
            repository(owner: $owner, name: $name) {
                discussions(
                    first: 100
                    categoryId: $categoryId
                    answered: false
                    orderBy: {field: UPDATED_AT, direction: DESC}
                    after: $cursor
                ) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                        id number title body closed createdAt updatedAt
                        author { login }
                        labels(first: 10) { nodes { name } }
                        comments(first: 1) { totalCount }
                        upvoteCount
                    }
                }
            }
        }
        """
        all_discussions: List[Dict] = []
        cursor = None

        for page in range(max_pages):
            if shutdown.requested:
                break
            variables = {"owner": owner, "name": repo, "categoryId": category_id, "cursor": cursor}
            ck   = f"discussions:{owner}/{repo}:{page}:{cursor}"
            data = self._gql(query, variables, cache_key=ck)
            if not data:
                break

            discussions = (data.get("repository") or {}).get("discussions", {})
            nodes       = discussions.get("nodes", [])
            page_info   = discussions.get("pageInfo", {})

            for d in nodes:
                if label:
                    labels = [l["name"] for l in (d.get("labels") or {}).get("nodes", [])]
                    if label not in labels:
                        continue
                if not d.get("closed"):
                    all_discussions.append(d)

            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            time.sleep(PAGE_DELAY)

        return all_discussions

    # ── Comment fetcher ────────────────────────────────────────────────────────

    def get_discussion_comments(self, owner: str, repo: str, discussion_number: int) -> List[Dict]:
        ck = f"comments:{owner}/{repo}:{discussion_number}"
        cached = cache.get(ck)
        if cached is not None:
            return cached

        query = """
        query GetComments($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                discussion(number: $number) {
                    comments(first: 20) {
                        nodes { id body author { login } createdAt isAnswer }
                    }
                }
            }
        }
        """
        data   = self._gql(query, {"owner": owner, "name": repo, "number": discussion_number})
        result = (
            (data or {}).get("repository", {}).get("discussion", {})
                        .get("comments", {}).get("nodes", [])
        )
        cache.set(ck, result)
        return result

    # ── Post comment ───────────────────────────────────────────────────────────

    def create_discussion_comment(self, discussion_id: str, body: str) -> Optional[str]:
        if deduplicator.is_duplicate("post", discussion_id, body[:100]):
            logger.warning(f"Duplicate post prevented for discussion {discussion_id}")
            return None

        mutation = """
        mutation AddComment($discussionId: ID!, $body: String!) {
            addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
                comment { url id }
            }
        }
        """
        data = self._gql(mutation, {"discussionId": discussion_id, "body": body})
        if data:
            return (data.get("addDiscussionComment") or {}).get("comment", {}).get("url")
        return None

    def get_my_comments_in_discussion(
        self, owner: str, repo: str, discussion_number: int
    ) -> List[Dict]:
        query = """
        query GetComments($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                discussion(number: $number) {
                    comments(first: 100) {
                        nodes { id url isAnswer author { login } createdAt }
                    }
                }
            }
        }
        """
        data  = self._gql(query, {"owner": owner, "name": repo, "number": discussion_number})
        nodes = (
            (data or {}).get("repository", {}).get("discussion", {})
                        .get("comments", {}).get("nodes", [])
        )
        return [c for c in nodes if (c.get("author") or {}).get("login") == self.username]


# ─────────────────────────────────────────────────────────────────────────────
# Key manager
# ─────────────────────────────────────────────────────────────────────────────

class KeyManager:
    def __init__(self):
        self.openrouter_keys: List[str] = []
        self.current_key_index = 0
        self.key_stats: Dict = {}
        raw = os.getenv("OPENROUTER_KEYS", "")
        self.openrouter_keys = [k.strip() for k in raw.split(",") if k.strip()]
        for k in self.openrouter_keys:
            self.key_stats[k] = {
                "usage_count": 0, "errors": 0,
                "rate_limited_until": None, "last_used": None,
            }
        console.print(f"[green]Loaded {len(self.openrouter_keys)} OpenRouter key(s)[/green]")

    def get_next_key(self) -> Optional[str]:
        if not self.openrouter_keys:
            return None
        now = datetime.now(timezone.utc)
        for _ in range(len(self.openrouter_keys) * 2):
            key = self.openrouter_keys[self.current_key_index]
            self.current_key_index = (self.current_key_index + 1) % len(self.openrouter_keys)
            rl = self.key_stats[key]["rate_limited_until"]
            if rl is None or now > rl:
                self.key_stats[key]["rate_limited_until"] = None
                self.key_stats[key]["last_used"] = now
                return key
        return min(self.openrouter_keys, key=lambda k: self.key_stats[k]["rate_limited_until"] or datetime.min.replace(tzinfo=timezone.utc))

    def mark_rate_limited(self, key: str, retry_after: int = 60):
        if key in self.key_stats:
            self.key_stats[key]["rate_limited_until"] = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
            self.key_stats[key]["errors"] += 1

    def increment_usage(self, key: str):
        if key in self.key_stats:
            self.key_stats[key]["usage_count"] += 1


# ─────────────────────────────────────────────────────────────────────────────
# Stats tracker
# ─────────────────────────────────────────────────────────────────────────────

class StatsTracker:
    def __init__(self, max_answers: int = 500):
        self.stats_file  = "galaxy_brain_stats.json"
        self.backup_file = "galaxy_brain_stats.json.backup"
        self.max_answers = max_answers
        self.answered_ids: Set[str] = set()
        self.dirty = False
        self._backup_done_this_session = False
        self._cleanup_old_backups()
        self.stats = self._load()

    def _cleanup_old_backups(self):
        try:
            for f in os.listdir("."):
                if f.startswith(f"{self.stats_file}.backup_"):
                    age = time.time() - os.path.getmtime(f)
                    if age > 86400 * 7:
                        os.remove(f)
        except Exception:
            pass

    def _rolling_backup(self):
        if self._backup_done_this_session:
            return
        if not os.path.exists(self.stats_file):
            return
        try:
            import shutil
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(self.stats_file, f"{self.stats_file}.backup_{ts}")
            shutil.copy2(self.stats_file, self.backup_file)
            backups = sorted([f for f in os.listdir(".") if f.startswith(f"{self.stats_file}.backup_")])
            for old in backups[:-5]:
                try:
                    os.remove(old)
                except Exception:
                    pass
            self._backup_done_this_session = True
        except Exception:
            pass

    def _load(self) -> Dict:
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file) as f:
                    data = json.load(f)
                self.answered_ids = (
                    set(data.get("answered_discussion_ids", []))
                    | {a["id"] for a in data.get("answers", [])}
                )
                if len(data.get("answers", [])) > self.max_answers:
                    data["answers"] = data["answers"][-self.max_answers:]
                    self.dirty = True
                data.setdefault("answers", [])
                data.setdefault("total_answers",    len(data["answers"]))
                data.setdefault("accepted_answers", sum(1 for a in data["answers"] if a.get("accepted")))
                data.setdefault("lifetime_total",   data["total_answers"])
                data.setdefault("version", "6.0")
                console.print(f"[green]Loaded stats - {len(self.answered_ids)} already answered[/green]")
                return data
            except Exception as e:
                console.print(f"[red]Error loading stats: {e}[/red]")
                if os.path.exists(self.backup_file):
                    try:
                        with open(self.backup_file) as f:
                            return json.load(f)
                    except Exception:
                        pass
        return {
            "total_answers": 0, "lifetime_total": 0, "accepted_answers": 0,
            "answers": [], "answered_discussion_ids": [],
            "start_date": datetime.now(timezone.utc).isoformat(),
            "last_update": datetime.now(timezone.utc).isoformat(),
            "version": "6.0",
        }

    def _save(self):
        if not self.dirty:
            return
        self._rolling_backup()
        save = self.stats.copy()
        save["answered_discussion_ids"] = list(self.answered_ids)
        save["last_update"] = datetime.now(timezone.utc).isoformat()
        tmp = f"{self.stats_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(save, f, indent=2, default=str)
            os.replace(tmp, self.stats_file)
            self.dirty = False
        except Exception as e:
            console.print(f"[red]Error saving stats: {e}[/red]")

    def has_answered(self, discussion_id: str) -> bool:
        return discussion_id in self.answered_ids

    def add_answer(self, discussion_id, discussion_number, title, url,
                   answer_preview, repo_owner, repo_name):
        if self.has_answered(discussion_id):
            return False
        self.answered_ids.add(discussion_id)
        self.stats["total_answers"]  += 1
        self.stats["lifetime_total"]  = self.stats.get("lifetime_total", 0) + 1
        self.stats["answers"].append({
            "id": discussion_id, "number": discussion_number,
            "title": title, "repo_owner": repo_owner, "repo_name": repo_name,
            "url": url, "answer_preview": answer_preview[:100],
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "accepted": False, "accepted_at": None, "checked_count": 0,
        })
        if len(self.stats["answers"]) > self.max_answers:
            self.stats["answers"] = self.stats["answers"][-self.max_answers:]
        self.dirty = True
        self._save()
        return True

    def mark_accepted(self, discussion_id: str) -> bool:
        for a in self.stats["answers"]:
            if a["id"] == discussion_id and not a["accepted"]:
                a["accepted"]    = True
                a["accepted_at"] = datetime.now(timezone.utc).isoformat()
                self.stats["accepted_answers"] += 1
                self.dirty = True
                self._save()
                return True
        return False

    def get_pending(self, max_age_days: int = 30) -> List[Dict]:
        cutoff  = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        results = []
        for a in self.stats["answers"]:
            if a["accepted"]:
                continue
            dt = datetime.fromisoformat(a["posted_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > cutoff:
                results.append(a)
        return results

    def badge_progress(self) -> Dict:
        acc   = self.stats["accepted_answers"]
        total = self.stats["total_answers"]
        if   acc >= 32: tier, nxt = "Trophy Galaxy Brain Master (Complete!)", None
        elif acc >= 16: tier, nxt = "Brain Galaxy Brain Expert",        32 - acc
        elif acc >=  8: tier, nxt = "Star Galaxy Brain Specialist",    16 - acc
        elif acc >=  2: tier, nxt = "Seedling Galaxy Brain Achiever",   8 - acc
        else:           tier, nxt = "Books No badge yet",                2 - acc
        return {
            "accepted": acc, "tier": tier, "next_milestone": nxt,
            "total_answers": total,
            "lifetime_total": self.stats.get("lifetime_total", total),
            "acceptance_rate": (acc / total * 100) if total else 0,
        }

    def display(self):
        p = self.badge_progress()
        t = Table(title="Galaxy Brain Badge Progress", style="cyan")
        t.add_column("Metric", style="bold cyan")
        t.add_column("Value",  style="green")
        t.add_row("Total Answers Posted", str(p["total_answers"]))
        t.add_row("Lifetime Total",       str(p["lifetime_total"]))
        t.add_row("Accepted Answers",     f"[bold green]{p['accepted']}[/bold green]")
        t.add_row("Acceptance Rate",      f"{p['acceptance_rate']:.1f}%")
        t.add_row("Current Tier",         p["tier"])
        if p["next_milestone"]:
            t.add_row("Next Milestone", f"{p['next_milestone']} more needed")
        console.print(t)

    def display_by_org(self):
        org_totals: Dict[str, Dict] = defaultdict(lambda: {"total": 0, "accepted": 0})
        for a in self.stats["answers"]:
            key = f"{a.get('repo_owner', '?')}/{a.get('repo_name', '?')}"
            org_totals[key]["total"] += 1
            if a.get("accepted"):
                org_totals[key]["accepted"] += 1
        if not org_totals:
            return
        t = Table(title="Answers by Org/Repo", style="cyan")
        t.add_column("Org/Repo",  style="bold white")
        t.add_column("Answered",  style="cyan")
        t.add_column("Accepted",  style="green")
        t.add_column("Rate",      style="yellow")
        for key, v in sorted(org_totals.items()):
            rate = f"{v['accepted'] / v['total'] * 100:.0f}%" if v["total"] else "0%"
            t.add_row(key, str(v["total"]), str(v["accepted"]), rate)
        console.print(t)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-modal: image fetcher
# ─────────────────────────────────────────────────────────────────────────────

def _is_vision_model(model: str) -> bool:
    m = model.lower()
    return any(hint in m for hint in _VISION_MODEL_HINTS)


def fetch_image_as_b64(url: str, session: Optional[requests.Session] = None) -> Optional[Dict]:
    """
    Download an image URL and return {"data": "<b64>", "media_type": "image/png"}.
    Returns None if the image is too large, not an image, or fetch fails.
    """
    if not ENABLE_IMAGE_ANALYSIS:
        return None
    try:
        req = session or requests
        r = req.get(url, timeout=LINK_FETCH_TIMEOUT, stream=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; GalaxyBrainBot/1.0)"})
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if not ct.startswith("image/"):
            return None
        media_type = ct.split(";")[0].strip()
        chunks = []
        total  = 0
        for chunk in r.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > IMAGE_MAX_BYTES:
                logger.debug(f"Image too large (>{IMAGE_MAX_BYTES}): {url}")
                return None
            chunks.append(chunk)
        data = base64.b64encode(b"".join(chunks)).decode()
        return {"data": data, "media_type": media_type}
    except Exception as e:
        logger.debug(f"Image fetch failed {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Multi-modal: link content fetcher
# ─────────────────────────────────────────────────────────────────────────────

_GITHUB_ISSUE_RE   = re.compile(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)")
_GITHUB_PR_RE      = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")
_GITHUB_FILE_RE    = re.compile(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)")
_GITHUB_GIST_RE    = re.compile(r"https://gist\.github\.com/([^/]+)/([a-f0-9]+)")
_PASTEBIN_RE       = re.compile(r"https://pastebin\.com/([A-Za-z0-9]+)$")
_NPM_RE            = re.compile(r"https://www\.npmjs\.com/package/([^/?\s]+)")
_PYPI_RE           = re.compile(r"https://pypi\.org/project/([^/?\s]+)")
_STACKOVERFLOW_RE  = re.compile(r"https://stackoverflow\.com/questions/(\d+)")

_SKIP_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "linkedin.com", "reddit.com", "youtube.com",
}


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def fetch_link_content(url: str, github_token: Optional[str] = None) -> Optional[str]:
    """
    Fetch the meaningful text content of a URL.
    Handles GitHub issues/PRs/files specially; generic HTML otherwise.
    Returns a short plain-text summary (<= LINK_FETCH_MAX_CHARS).
    """
    if not ENABLE_LINK_FETCH:
        return None
    if _domain(url) in _SKIP_DOMAINS:
        return None

    try:
        headers_gh = {"Accept": "application/vnd.github.v3+json"}
        if github_token:
            headers_gh["Authorization"] = f"Bearer {github_token}"

        m = _GITHUB_ISSUE_RE.match(url)
        if m:
            owner, repo, num = m.groups()
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{num}",
                headers=headers_gh, timeout=LINK_FETCH_TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                text = f"[GitHub Issue #{num}] {d.get('title','')}\n{(d.get('body') or '')[:1200]}"
                return text[:LINK_FETCH_MAX_CHARS]

        m = _GITHUB_PR_RE.match(url)
        if m:
            owner, repo, num = m.groups()
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}",
                headers=headers_gh, timeout=LINK_FETCH_TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                text = f"[GitHub PR #{num}] {d.get('title','')}\n{(d.get('body') or '')[:1200]}"
                return text[:LINK_FETCH_MAX_CHARS]

        m = _GITHUB_FILE_RE.match(url)
        if m:
            owner, repo, ref, path = m.groups()
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
            r = requests.get(raw_url, timeout=LINK_FETCH_TIMEOUT,
                             headers={"User-Agent": "GalaxyBrainBot/1.0"})
            if r.status_code == 200:
                text = r.text[:LINK_FETCH_MAX_CHARS]
                return f"[GitHub file: {path}]\n{text}"

        m = _PASTEBIN_RE.match(url)
        if m:
            paste_id = m.group(1)
            r = requests.get(f"https://pastebin.com/raw/{paste_id}",
                             timeout=LINK_FETCH_TIMEOUT,
                             headers={"User-Agent": "GalaxyBrainBot/1.0"})
            if r.status_code == 200:
                return f"[Pastebin]\n{r.text[:LINK_FETCH_MAX_CHARS]}"

        m = _GITHUB_GIST_RE.match(url)
        if m:
            user, gist_id = m.groups()
            r = requests.get(f"https://api.github.com/gists/{gist_id}",
                             headers=headers_gh, timeout=LINK_FETCH_TIMEOUT)
            if r.status_code == 200:
                files = r.json().get("files", {})
                parts = []
                for fname, fdata in list(files.items())[:3]:
                    content = fdata.get("content") or fdata.get("truncated_content") or ""
                    parts.append(f"-- {fname} --\n{content[:800]}")
                return f"[Gist]\n" + "\n".join(parts)[:LINK_FETCH_MAX_CHARS]

        m = _NPM_RE.match(url)
        if m:
            pkg = m.group(1)
            r = requests.get(f"https://registry.npmjs.org/{pkg}/latest",
                             timeout=LINK_FETCH_TIMEOUT,
                             headers={"User-Agent": "GalaxyBrainBot/1.0"})
            if r.status_code == 200:
                d = r.json()
                desc = d.get("description", "")
                vers = d.get("version", "")
                return f"[npm: {pkg} v{vers}] {desc}"[:LINK_FETCH_MAX_CHARS]

        m = _PYPI_RE.match(url)
        if m:
            pkg = m.group(1)
            r = requests.get(f"https://pypi.org/pypi/{pkg}/json",
                             timeout=LINK_FETCH_TIMEOUT,
                             headers={"User-Agent": "GalaxyBrainBot/1.0"})
            if r.status_code == 200:
                info = r.json().get("info", {})
                desc = (info.get("summary") or "")[:400]
                vers = info.get("version", "")
                return f"[PyPI: {pkg} v{vers}] {desc}"[:LINK_FETCH_MAX_CHARS]

        # Generic HTML fallback
        r = requests.get(
            url, timeout=LINK_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GalaxyBrainBot/1.0)"},
            stream=True,
        )
        ct = r.headers.get("Content-Type", "")
        if "text/html" not in ct and "text/plain" not in ct:
            return None
        raw_text = ""
        size = 0
        for chunk in r.iter_content(chunk_size=8192, decode_unicode=True):
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="ignore")
            raw_text += chunk
            size += len(chunk)
            if size > 40_000:
                break
        raw_text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", raw_text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:LINK_FETCH_MAX_CHARS]

    except Exception as e:
        logger.debug(f"Link fetch failed {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Multi-modal: extract URLs and images from discussion body
# ─────────────────────────────────────────────────────────────────────────────

_URL_RE      = re.compile(r"https?://[^\s\)\]\>\"\'>]+")
_MD_IMAGE_RE = re.compile(r"!\[.*?\]\((https?://[^\)]+)\)")
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']?(https?://[^\s"\'>/]+[^\s"\'>;]*)', re.IGNORECASE)


def extract_urls_from_text(text: str) -> Tuple[List[str], List[str]]:
    """
    Returns (image_urls, link_urls) — images separated from regular links.
    Deduplicates and caps at MAX_IMAGES_PER_POST / MAX_LINKS_PER_POST.
    """
    if not text:
        return [], []

    image_urls: List[str] = []
    seen_img: Set[str]    = set()
    link_urls: List[str]  = []
    seen_lnk: Set[str]    = set()

    for url in _MD_IMAGE_RE.findall(text) + _HTML_IMG_RE.findall(text):
        url = url.rstrip(".,;:)")
        if url not in seen_img and len(image_urls) < MAX_IMAGES_PER_POST:
            image_urls.append(url)
            seen_img.add(url)

    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:)")
        ext = url.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
        if ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
            if url not in seen_img and len(image_urls) < MAX_IMAGES_PER_POST:
                image_urls.append(url)
                seen_img.add(url)
        else:
            if url not in seen_lnk and url not in seen_img and len(link_urls) < MAX_LINKS_PER_POST:
                link_urls.append(url)
                seen_lnk.add(url)

    return image_urls, link_urls


# ─────────────────────────────────────────────────────────────────────────────
# CoC analyzer
# ─────────────────────────────────────────────────────────────────────────────

def extract_coc_rules(coc_text: str) -> str:
    if not coc_text:
        return ""
    lines = coc_text.splitlines()
    keep  = []
    rule_keywords = [
        "do not", "don't", "avoid", "must", "should", "please", "expected",
        "prohibited", "not allowed", "required", "refrain", "ensure",
        "harassment", "inclusive", "respectful", "welcoming", "constructive",
        "spam", "self-promotion", "off-topic", "offensive", "abuse",
        "be kind", "be patient", "be respectful", "be constructive",
    ]
    for line in lines:
        line_clean = line.strip()
        if not line_clean or len(line_clean) < 15:
            continue
        if any(kw in line_clean.lower() for kw in rule_keywords):
            line_clean = re.sub(r"^[\*\-\d\.\s]+", "", line_clean).strip()
            if line_clean and len(line_clean) > 15:
                keep.append(f"- {line_clean}")
        if len(keep) >= 10:
            break
    return "\n".join(keep)


# ─────────────────────────────────────────────────────────────────────────────
# Comment summarizer
# ─────────────────────────────────────────────────────────────────────────────

def summarize_comments(comments: List[Dict]) -> str:
    if not comments:
        return ""
    lines = []
    for c in comments[:8]:
        author  = (c.get("author") or {}).get("login", "someone")
        body    = (c.get("body") or "").strip()
        if not body:
            continue
        preview = body[:300].replace("\n", " ")
        if len(body) > 300:
            preview += "..."
        lines.append(f"- {author}: {preview}")
    return "\n".join(lines) if lines else ""


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder — IMPROVED
# ─────────────────────────────────────────────────────────────────────────────

def build_answer_prompt(
    title: str,
    body: str,
    existing_comments: List[Dict] = None,
    coc_text: Optional[str] = None,
    repo_context: str = "",
    link_contexts: Optional[List[str]] = None,
    image_descriptions: Optional[List[str]] = None,
) -> str:
    coc_rules_extracted = extract_coc_rules(coc_text) if coc_text else ""
    if coc_rules_extracted:
        coc_section = f"""Community rules for this repo:
{coc_rules_extracted}
Stay within these. Don't self-promote, don't be preachy."""
    else:
        coc_section = "Be direct. No self-promotion. No lectures."

    comment_summary = summarize_comments(existing_comments or [])
    if comment_summary:
        comments_section = f"""What others have said so far:
{comment_summary}

Don't repeat them. Either build on what they said or correct what they got wrong."""
    else:
        comments_section = "No one has replied yet — you're first."

    context_line = f"Repo: {repo_context}" if repo_context else ""

    link_section = ""
    if link_contexts:
        joined = "\n\n".join(f"[Link {i+1}]\n{ctx}" for i, ctx in enumerate(link_contexts))
        link_section = f"""
The question includes external links. Here's what they contain:
{joined}

Work this in naturally if it's relevant. Don't say you fetched it."""

    image_section = ""
    if image_descriptions:
        joined = "\n".join(f"- {desc}" for desc in image_descriptions)
        image_section = f"""
Images in the question:
{joined}

Reference what you see directly (e.g. "your error shows X" or "that config line is wrong")."""

    return f"""\
You are a senior software engineer — 10+ years of open source, backend, DevOps, and GitHub. \
You have personally hit this kind of problem before. Write exactly like you'd respond to a \
colleague on Slack: direct, specific, opinionated. {context_line}

{coc_section}

{comments_section}
{link_section}{image_section}

OUTPUT RULES — READ THESE CAREFULLY:
1. Output ONLY the answer text. Nothing else. No preamble, no meta-commentary.
2. Do NOT start with "Let's", "Here's", "Sure", "Great", or any opener phrase.
3. Do NOT write "Answer:", "My answer:", "First word:", or any self-referential label.
4. Do NOT narrate what you're about to do. Just do it.
5. The very first character of your response must be the start of the actual answer.
6. Do NOT wrap the answer in quotes. Output it raw.
7. If you feel the urge to write an intro sentence — delete it and start with the second sentence.

PERSONA — YOU ARE A REAL PERSON:
- You've personally hit this kind of problem before. Reference that when it's natural.
- "I ran into this same thing last year", "I'd check X first", "I usually do Y"
- Have a take. Don't just list options — pick one and say why.
- If you disagree with the existing comments, say so and explain why.
- Acknowledge when you're not 100% sure: "not certain, but..." then still commit to an answer.

STYLE — VARY YOUR SENTENCE RHYTHM:
- Mix short punchy sentences with longer explanations. Not every sentence the same length.
- Name the exact thing: command, flag, file path, config key, function name, line number.
- No bullets unless it's a numbered step sequence. No headers. No bold.
- Aim for 80–150 words. Stop when you're done. No sign-offs.
- Code snippets are fine if they're the clearest way to explain something.

WORDS AND PHRASES THAT WILL GET THIS FLAGGED AS AI — NEVER USE:
"It's worth noting", "It's important to", "In order to", "This serves as",
"functions as", "leverage", "utilize", "enhance", "seamlessly", "robustly",
"comprehensively", "as an AI", "I hope that helps", "feel free to",
"don't hesitate", "does that make sense", "Let's craft", "Let me think",
"straightforward", "simply put", "to be fair", "the key takeaway",
"pro tip", "heads up", "with that said", "that being said", "of course",
"The answer is:", "To answer your question", "We need to", "The first word",
starting with a quote character, wrapping the answer in quotes.

WHAT GETS ANSWERS MARKED AS ACCEPTED:
- Solves the actual problem, not a rephrased version of it
- Specific enough to act on immediately
- Sounds like someone who has done this before, not someone googling it
- Takes a position instead of listing every possible option

---
Question title: {title}

Question body:
{body[:BODY_TRUNCATE_CHARS]}
---

Reply (senior dev, plain text, no opener, no labels, just the answer):"""


# ─────────────────────────────────────────────────────────────────────────────
# Post-processor — IMPROVED
# ─────────────────────────────────────────────────────────────────────────────

_REASONING_SIGNALS = re.compile(
    r"\b(we need to|the instruction|so the first word|let me think|"
    r"we must|we should|we have to|so we say|so i should|so i will|"
    r"the question asks|so my answer|now i need|first word must|"
    r"must be the answer|need to produce|need to answer|"
    r"start with something|provide the answer|the answer is:|"
    r"let'?s craft|let'?s write the|thus answer|"
    r"under \d+ words|i will answer|i will write|i'll write|"
    r"the reply should|my reply is|here is the answer)\b",
    re.IGNORECASE,
)

def post_process_answer(answer: str) -> str:
    """Strip AI artifacts, reasoning bleed, labels, and filler from the generated answer."""

    if not answer:
        return answer

    # ── 1. Strip explicit answer labels/markers ────────────────────────────────
    # "Answer: ...", "My answer: ...", "Final answer: ...", "Here is my answer: ..."
    answer = re.sub(
        r"^(?:(?:final\s+)?answer|my answer|here(?:'s| is)(?: my)? answer|reply)\s*[:\-]\s*",
        "", answer, flags=re.IGNORECASE,
    ).strip()

    # ── 2. Strip opening quotes (some models wrap the answer in "..." or '...') ──
    # e.g. "The fix is to..." or 'Run pip install first.'
    answer = re.sub(r'^["\'](.+)["\']$', r'\1', answer, flags=re.DOTALL).strip()

    # ── 3. Strip "Let's craft:" / "Let's write:" preamble ─────────────────────
    answer = re.sub(
        r"^(?:let'?s\s+(?:craft|write|think|consider|look at|start|begin|answer)[^:\n]*[:.]?\s*)+",
        "", answer, flags=re.IGNORECASE,
    ).strip()

    # ── 4. Strip "Thus answer:" / "So the answer is:" fragments ──────────────
    answer = re.sub(
        r"^(?:thus|so|therefore|hence)\s+(?:the\s+)?(?:answer|reply)\s*[:\-]\s*",
        "", answer, flags=re.IGNORECASE,
    ).strip()

    # ── 5. Detect and remove reasoning paragraphs ─────────────────────────────
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", answer) if p.strip()]
    if len(paragraphs) > 1:
        first_real = 0
        for i, para in enumerate(paragraphs):
            if _REASONING_SIGNALS.search(para):
                first_real = i + 1
            else:
                break
        if 0 < first_real < len(paragraphs):
            answer = "\n\n".join(paragraphs[first_real:])

    # ── 6. Sentence-level reasoning cleanup (single-block monologue) ──────────
    if _REASONING_SIGNALS.search(answer):
        sentences = re.split(r"(?<=[.!?])\s+", answer)
        clean = [s for s in sentences if not _REASONING_SIGNALS.search(s) and len(s) > 20]
        if clean:
            answer = " ".join(clean)

    # ── 7. Strip opener filler phrases ────────────────────────────────────────
    opener_patterns = [
        r"^Great question[.!]\s*",
        r"^Thanks for (asking|your question|posting)[.!]\s*",
        r"^Sure[,!]\s*",
        r"^Of course[,!]\s*",
        r"^Absolutely[,!]\s*",
        r"^Certainly[,!]\s*",
        r"^That'?s? (a )?(great|good|excellent) (point|question)[.!]\s*",
        r"^You'?re (absolutely )?right[.!]\s*",
        r"^To answer your question[,:]?\s*",
        r"^(?:Here'?s?|This is) (?:my |the |a )?(?:answer|reply|response)[:.]\s*",
        r"^The answer (?:is|to this is)[:.]\s*",
    ]
    for pattern in opener_patterns:
        answer = re.sub(pattern, "", answer, flags=re.IGNORECASE)
    answer = answer.strip()

    # ── 8. Strip trailing filler ──────────────────────────────────────────────
    filler_endings = [
        r"\n*I hope (that )?this helps[.!]*\s*$",
        r"\n*Feel free to (ask|reach out|let me know)[^.]*[.!]*\s*$",
        r"\n*Let me know if you (need|have)[^.]*[.!]*\s*$",
        r"\n*Don't hesitate to[^.]*[.!]*\s*$",
        r"\n*If you have (any )?more questions[^.]*[.!]*\s*$",
        r"\n*Good luck[.!]*\s*$",
        r"\n*Hope this (helps|works)[.!]*\s*$",
        r"\n*Best of luck[.!]*\s*$",
        r"\n*Happy (coding|building|developing|to help)[.!]*\s*$",
        r"\n*Best[,.]?\s*$",
        r"\n*Cheers[,.]?\s*$",
        r"\n*Thanks for (sharing|posting|asking)[^.]*[.!]*\s*$",
        r"\n*Does that (make sense|help|answer)[^.]*[.!?]*\s*$",
    ]
    for pattern in filler_endings:
        answer = re.sub(pattern, "", answer, flags=re.IGNORECASE).rstrip()

    # ── 9. Inline AI-isms ─────────────────────────────────────────────────────
    answer = answer.replace("\u2014", " - ").replace("\u2013", " - ")  # em/en dashes

    inline_replacements = [
        (r"It'?s (worth noting|important to note|worth mentioning) that\s*", ""),
        (r"\bIn order to\b", "To"),
        (r"\bdue to the fact that\b", "because"),
        (r"\b(As a matter of fact|Basically|To be honest|Frankly|Essentially),?\s+", ""),
        (r"\bserves as a\b", "is a"),
        (r"\bfunctions as a\b", "is a"),
        (r"\bstands as a\b", "is a"),
        (r"\bacts as a\b", "is a"),
        (r"\bleverag(e|ing|es|ed)\b", r"us\1"),
        (r"\butiliz(e|ing|es|ed)\b", r"us\1"),
        (r"\benhance(s|d|ment)?\b", r"improve\1"),
        (r"\bseamless(ly)?\b", r"smooth\1"),
        (r"\brobust\b", "solid"),
        (r"\bcomprehensive(ly)?\b", r"thorough\1"),
        (r"\bnuanced\b", "detailed"),
        (r"\bgroundbreaking\b", "significant"),
        (r"\bpivotal\b", "key"),
        (r"\bdelve\b", "dig"),
        (r"\bfoster\b", "build"),
        (r"\bcultivate\b", "build"),
        (r"\bensure that\b", "make sure"),
        (r"\bAt its core,?\s*", ""),
        (r"\bIn conclusion,?\s*", ""),
        (r"\bTo summarize,?\s*", ""),
        (r"\bAs an AI\b[^.]*\.\s*", ""),
        (r"\bIt is (worth|important) to note that\b", ""),
        (r"\btailored\b", "custom"),
        (r"\bpowered by\b", "using"),
        # 2025-era AI tells
        (r"\bto be fair,?\s*", ""),
        (r"\bsimply put,?\s*", ""),
        (r"\bin practice,?\s*", ""),
        (r"\bstraightforward(ly)?\b", r"simple\1" if False else "simple"),
        (r"\bstraightforward\b", "simple"),
        (r"\bstraightforwardly\b", "simply"),
        (r"\bthink of it as\b", "it's like"),
        (r"\bthe good news is,?\s*", ""),
        (r"\bthe bad news is,?\s*", ""),
        (r"\bworth (exploring|considering|noting)\b", ""),
        (r"\bit'?s also worth\b[^.]*\.\s*", ""),
        (r"\bright\?\s*", ""),
        (r"\bof course,?\s*", ""),
        (r"\bneedless to say,?\s*", ""),
        (r"\bwith that said,?\s*", ""),
        (r"\ball that said,?\s*", ""),
        (r"\bthat being said,?\s*", ""),
        (r"\bpro tip:?\s*", ""),
        (r"\bquick note:?\s*", ""),
        (r"\bheads up:?\s*", ""),
        (r"\bin essence,?\s*", ""),
        (r"\bput simply,?\s*", ""),
        (r"\blong story short,?\s*", ""),
        (r"\bthe key (here|takeaway) is\b", ""),
        (r"\boptimal(ly)?\b", r"best\1" if False else "best"),
        (r"\boptimal\b", "best"),
        (r"\boptimally\b", "best"),
    ]
    for pattern, replacement in inline_replacements:
        answer = re.sub(pattern, replacement, answer, flags=re.IGNORECASE)

    # ── 10. Final cleanup ─────────────────────────────────────────────────────
    answer = re.sub(r"  +", " ", answer).strip()

    # Strip a leading quote from the very first character if it crept back in
    if answer and answer[0] in ('"', "'", "\u201c", "\u2018"):
        answer = answer[1:].lstrip()
    if answer and answer[-1] in ('"', "'", "\u201d", "\u2019"):
        answer = answer[:-1].rstrip()

    return answer


# ─────────────────────────────────────────────────────────────────────────────
# Answer validator — catch responses that still smell like AI slop
# ─────────────────────────────────────────────────────────────────────────────

_SLOP_SIGNALS = re.compile(
    r"^(let'?s |here'?s |here is |to answer |the answer is |my answer |thus |"
    r"sure[,!] |of course |absolutely[,!] |certainly[,!] |great question)",
    re.IGNORECASE,
)

_STILL_REASONING = re.compile(
    r"\b(we need to (answer|produce|write)|the instruction says|"
    r"first word must be|must be the answer|need to produce answer|"
    r"so the first word|let me think|we must (not|add|answer)|"
    r"let'?s craft|thus answer|under \d+ words)\b",
    re.IGNORECASE,
)


def is_valid_answer(answer: str) -> Tuple[bool, str]:
    """Returns (is_valid, rejection_reason)."""
    if not answer or len(answer) < ANSWER_MIN_CHARS:
        return False, f"too short ({len(answer)} chars)"
    if _STILL_REASONING.search(answer[:400]):
        return False, "reasoning bleed detected"
    if _SLOP_SIGNALS.search(answer[:80]):
        return False, "AI opener detected"
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Target manager
# ─────────────────────────────────────────────────────────────────────────────

def _load_hardcoded_targets() -> List[Tuple[str, str]]:
    raw = os.getenv("DISCUSSION_TARGETS", "").strip()
    targets = []
    if raw:
        for part in raw.split(","):
            part = part.strip()
            segs = part.split(":")
            if len(segs) >= 2:
                targets.append((segs[0].strip(), segs[1].strip()))
    return targets


# ─────────────────────────────────────────────────────────────────────────────
# Health check server (optional background thread)
# ─────────────────────────────────────────────────────────────────────────────

def _start_health_server(port: int, bot_ref):
    import http.server
    import json as _json

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path in ("/health", "/"):
                body = _json.dumps({
                    "status": "ok",
                    "shutdown_requested": shutdown.requested,
                    "cache": cache.stats(),
                    "circuit_breakers": {
                        "github":     _cb_github.status(),
                        "openrouter": _cb_openrouter.status(),
                    },
                }).encode()
            elif self.path == "/metrics":
                p = bot_ref.stats.badge_progress()
                body = _json.dumps({
                    "total_answers":    p["total_answers"],
                    "accepted_answers": p["accepted"],
                    "acceptance_rate":  p["acceptance_rate"],
                    "tier":             p["tier"],
                    "model_performance": model_tracker.summary(),
                }).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    console.print(f"[dim]Health server on :{port}  (/health  /metrics)[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# Main Bot
# ─────────────────────────────────────────────────────────────────────────────

class GalaxyBrainBot:

    def __init__(self):
        self.github_token    = os.getenv("GITHUB_TOKEN")
        self.github_username = os.getenv("GITHUB_USERNAME")
        if not self.github_token or not self.github_username:
            console.print("[red]Missing GITHUB_TOKEN or GITHUB_USERNAME in .env[/red]")
            sys.exit(1)

        self.api         = GitHubDiscussionsAPI(self.github_token, self.github_username)
        self.key_manager = KeyManager()
        self.stats       = StatsTracker(max_answers=500)
        self.webhook     = WebhookNotifier()

        self.max_answers  = min(int(os.getenv("MAX_ANSWERS_PER_SESSION", "10")), 100)
        self.delay        = int(os.getenv("DELAY_BETWEEN_ANSWERS", "30"))
        self.auto_post    = os.getenv("AUTO_APPROVE_ANSWERS", "false").lower() == "true"
        self.label_filter = os.getenv("LABEL_FILTER", "").strip() or None

        self.use_discovery       = True
        self.discovery_topics    = DISCOVERY_TOPICS
        self.discovery_min_stars = DISCOVERY_MIN_STARS
        self.discovery_max_repos = DISCOVERY_MAX_REPOS

        if HEALTH_CHECK_PORT > 0:
            _start_health_server(HEALTH_CHECK_PORT, self)

        console.print(f"[green]Bot ready — posting as: {self.github_username}[/green]")
        if self.auto_post:
            console.print("[yellow]AUTO_POST is ON[/yellow]")
        self.verbose = False

    def _build_target_list(self) -> List[Tuple[str, str]]:
        hardcoded = _load_hardcoded_targets()
        seen: Set[str] = {f"{o}/{r}" for o, r in hardcoded}
        targets = list(hardcoded)

        if self.use_discovery:
            discovered = self.api.discover_repos_with_discussions(
                self.discovery_topics, self.discovery_min_stars, self.discovery_max_repos,
            )
            for o, r in discovered:
                key = f"{o}/{r}"
                if key not in seen:
                    seen.add(key)
                    targets.append((o, r))

        console.print(
            f"[cyan]Total target repos: {len(targets)} "
            f"({len(hardcoded)} hardcoded + {len(targets)-len(hardcoded)} discovered)[/cyan]"
        )
        return targets

    def generate_answer(
        self,
        title: str,
        body: str,
        existing_comments: List[Dict] = None,
        coc_text: Optional[str] = None,
        repo_context: str = "",
    ) -> Optional[str]:

        # ── Multi-modal enrichment ─────────────────────────────────────────
        image_urls, link_urls = extract_urls_from_text(body or "")

        link_contexts: List[str] = []
        if ENABLE_LINK_FETCH and link_urls:
            for url in link_urls:
                ck = f"link:{url}"
                cached = cache.get(ck)
                if cached is not None:
                    if cached:
                        link_contexts.append(cached)
                    continue
                ctx = fetch_link_content(url, github_token=self.github_token)
                cache.set(ck, ctx or "")
                if ctx:
                    link_contexts.append(ctx)
                    console.print(f"  [dim]Fetched link: {url[:60]}[/dim]")

        fetched_images: List[Dict] = []
        if ENABLE_IMAGE_ANALYSIS and image_urls:
            for url in image_urls:
                ck = f"img:{url}"
                cached = cache.get(ck)
                if cached is not None:
                    if cached:
                        fetched_images.append(cached)
                    continue
                img = fetch_image_as_b64(url)
                cache.set(ck, img or {})
                if img:
                    fetched_images.append(img)
                    console.print(f"  [dim]Fetched image ({img['media_type']}): {url[:60]}[/dim]")

        prompt = build_answer_prompt(
            title=title, body=body,
            existing_comments=existing_comments or [],
            coc_text=coc_text, repo_context=repo_context,
            link_contexts=link_contexts if link_contexts else None,
            image_descriptions=None,
        )

        if not _cb_openrouter.allow():
            logger.warning("OpenRouter circuit breaker OPEN — skipping generation")
            return None

        if self.verbose:
            console.print("\n[bold magenta]--- VERBOSE: Full Prompt ---[/bold magenta]")
            console.print(prompt)
            console.print("[bold magenta]--- END PROMPT ---[/bold magenta]\n")

        max_key_rounds = max(len(self.key_manager.openrouter_keys), 1) * 2

        for key_round in range(max_key_rounds):
            if shutdown.requested:
                return None

            api_key = self.key_manager.get_next_key()
            if not api_key:
                return None

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/community/community",
                "X-Title":       "GitHub Community Helper",
            }

            ordered      = model_tracker.sorted_models(MODELS)
            tried_models: Set[str] = set()

            for model in ordered:
                if model in tried_models or shutdown.requested:
                    continue

                use_vision = fetched_images and _is_vision_model(model)
                if use_vision:
                    content: object = [{"type": "text", "text": prompt}]
                    for img in fetched_images:
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{img['media_type']};base64,{img['data']}"
                            },
                        })
                    messages = [{"role": "user", "content": content}]
                else:
                    messages = [{"role": "user", "content": prompt}]

                _rl_openrouter.wait_if_needed()
                t0 = time.time()
                try:
                    r = requests.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=headers,
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": 0.7,
                            "max_tokens": 400,
                        },
                        timeout=45,
                    )
                    latency = time.time() - t0

                    if r.status_code == 200:
                        self.key_manager.increment_usage(api_key)
                        choices = r.json().get("choices")
                        if not choices:
                            model_tracker.record(model, success=False, empty=True)
                            tried_models.add(model)
                            continue
                        raw = choices[0].get("message", {}).get("content", "").strip()
                        if self.verbose:
                            console.print(f"\n[bold magenta]--- VERBOSE: Raw ({model}) ---[/bold magenta]")
                            console.print(raw)
                            console.print("[bold magenta]--- END RAW ---[/bold magenta]\n")
                        if not raw:
                            model_tracker.record(model, success=False, empty=True)
                            tried_models.add(model)
                            continue

                        answer = post_process_answer(raw)

                        # Validate — reject if still bad
                        valid, reason = is_valid_answer(answer)
                        if not valid:
                            console.print(f"[yellow]{model}: rejected — {reason}[/yellow]")
                            model_tracker.record(model, success=False)
                            tried_models.add(model)
                            continue

                        if len(answer) > ANSWER_MAX_CHARS:
                            truncated = answer[:ANSWER_MAX_CHARS].rsplit("\n", 1)[0]
                            answer = truncated if truncated else answer[:ANSWER_MAX_CHARS]

                        model_tracker.record(model, success=True, latency=latency)
                        _cb_openrouter.record_success()
                        _rl_openrouter.reset_backoff()
                        vision_note = " +vision" if use_vision else ""
                        links_note  = f" +{len(link_contexts)}links" if link_contexts else ""
                        console.print(
                            f"[green]Generated via {model}{vision_note}{links_note} "
                            f"({len(answer)} chars, {latency:.1f}s)[/green]"
                        )
                        logger.info(
                            f"Answer generated: model={model} chars={len(answer)} "
                            f"latency={latency:.1f}s vision={use_vision} links={len(link_contexts)}"
                        )
                        return answer

                    elif r.status_code == 429:
                        model_tracker.record(model, success=False)
                        tried_models.add(model)
                        _rl_openrouter.backoff()
                        _cb_openrouter.record_failure()
                    elif r.status_code == 400 and use_vision:
                        logger.debug(f"{model}: 400 on vision payload, retrying text-only")
                        fetched_images = []
                        model_tracker.record(model, success=False)
                        tried_models.add(model)
                    else:
                        model_tracker.record(model, success=False)
                        tried_models.add(model)
                        _cb_openrouter.record_failure()

                except requests.Timeout:
                    model_tracker.record(model, success=False)
                    tried_models.add(model)
                except Exception as e:
                    console.print(f"[yellow]{model}: {e}[/yellow]")
                    model_tracker.record(model, success=False)
                    tried_models.add(model)
                time.sleep(MODEL_ATTEMPT_DELAY)

            self.key_manager.mark_rate_limited(api_key, RATE_LIMIT_ROTATE_AFTER)
            time.sleep(2)

        console.print("[red]Max retries reached — couldn't generate answer[/red]")
        logger.warning(f"Failed to generate answer for: {title[:60]}")
        return None

    def find_and_answer(self, targets: List[Tuple[str, str]]) -> int:
        all_discussions: List[Tuple[str, str, Dict]] = []

        for owner, repo in targets:
            if shutdown.requested:
                break
            console.print(f"\n[bold]Scanning {owner}/{repo}[/bold]")
            discussions = self.api.get_unanswered_discussions(owner, repo, label=self.label_filter)
            unseen = [d for d in discussions if not self.stats.has_answered(d["id"])]
            console.print(f"  [dim]{len(unseen)} candidates[/dim]")
            for d in unseen:
                all_discussions.append((owner, repo, d))

        def sort_key(item):
            _, _, d = item
            ts = d.get("createdAt", "")
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        all_discussions.sort(key=sort_key, reverse=True)
        console.print(
            f"\n[bold cyan]Total candidates: {len(all_discussions)} (sorted newest first)[/bold cyan]"
        )

        answered_count = 0
        skipped_count  = 0

        for idx, (owner, repo, d) in enumerate(all_discussions, 1):
            if shutdown.requested:
                console.print("[yellow]Shutdown requested — stopping[/yellow]")
                break
            if answered_count >= self.max_answers:
                break
            if self.stats.has_answered(d["id"]):
                continue

            # Per-repo cooldown check
            repo_key = f"{owner}/{repo}"
            if not repo_cooldown.is_cooled_down(repo_key):
                secs = repo_cooldown.seconds_remaining(repo_key)
                console.print(f"[dim]  Skipping #{d['number']}: repo on cooldown ({secs}s remaining)[/dim]")
                skipped_count += 1
                continue

            ok, reason = is_answerable(d)
            if not ok:
                console.print(f"[dim]  Skipping #{d['number']}: {reason}[/dim]")
                skipped_count += 1
                continue

            try:
                created_dt = datetime.fromisoformat(d["createdAt"].replace("Z", "+00:00"))
                age_str    = f"{(datetime.now(timezone.utc) - created_dt).days}d ago"
            except Exception:
                age_str = "?"

            comment_count = (d.get("comments") or {}).get("totalCount", 0)
            console.print(
                f"\n[{idx}/{len(all_discussions)}] {owner}/{repo} #{d['number']}  "
                f"{age_str} | {comment_count} comment(s)"
            )
            console.print(f"[bold]{d['title']}[/bold]")
            preview = (d.get("body") or "")[:200].replace("\n", " ")
            console.print(f"[dim]{preview}...[/dim]")

            comments     = self.api.get_discussion_comments(owner, repo, d["number"])
            coc_text     = self.api.fetch_code_of_conduct(owner, repo)
            repo_context = f"{owner}/{repo}"

            if comments:
                console.print(f"  [dim]Existing comments: {len(comments)}[/dim]")

            answer = self.generate_answer(
                title=d["title"], body=d.get("body", ""),
                existing_comments=comments, coc_text=coc_text, repo_context=repo_context,
            )

            if not answer:
                console.print("[red]Failed to generate, skipping[/red]")
                skipped_count += 1
                continue

            # Answer uniqueness check — reject if too similar to a past answer this session
            unique, sim = answer_uniqueness.is_unique(answer)
            if not unique:
                console.print(f"[yellow]Answer too similar to a past answer (similarity={sim:.2f}) — skipping[/yellow]")
                logger.info(f"Uniqueness rejected: similarity={sim:.2f} for #{d['number']}")
                skipped_count += 1
                continue

            console.print("\n[bold cyan]Generated Answer:[/bold cyan]")
            console.print(Panel(answer, border_style="blue", title=f"Answer ({len(answer)} chars)"))

            should_post = self.auto_post or Confirm.ask("\n[bold yellow]Post this answer?[/bold yellow]")

            if should_post:
                url = self.api.create_discussion_comment(d["id"], answer)
                if url:
                    self.stats.add_answer(
                        discussion_id=d["id"], discussion_number=d["number"],
                        title=d["title"], url=url, answer_preview=answer,
                        repo_owner=owner, repo_name=repo,
                    )
                    answer_uniqueness.register(answer)
                    repo_cooldown.record_post(repo_key)
                    answered_count += 1
                    console.print(f"[green]Posted ({answered_count}/{self.max_answers}): {url}[/green]")
                    logger.info(f"Posted: repo={owner}/{repo} #{d['number']} url={url}")
                    self.webhook.send_answer_notification(
                        title=d["title"], repo=repo_context,
                        answer_preview=answer, url=url, answer_length=len(answer),
                    )
                    if answered_count < self.max_answers and idx < len(all_discussions):
                        console.print(f"[dim]Waiting {self.delay}s...[/dim]")
                        time.sleep(self.delay)
                else:
                    console.print("[red]Failed to post[/red]")
                    skipped_count += 1
            else:
                console.print("[yellow]Skipped[/yellow]")
                skipped_count += 1

        console.print(f"\n[dim]Session: {answered_count} posted, {skipped_count} skipped[/dim]")
        return answered_count

    def check_accepted(self) -> int:
        console.print("[bold cyan]Checking for accepted answers[/bold cyan]")
        pending = self.stats.get_pending(max_age_days=30)
        if not pending:
            console.print("[dim]No pending answers[/dim]")
            return 0

        accepted_count = 0
        for idx, a in enumerate(pending, 1):
            if shutdown.requested:
                break
            org  = a.get("repo_owner", "community")
            repo = a.get("repo_name",  "community")
            try:
                comments = self.api.get_my_comments_in_discussion(org, repo, a["number"])
                for c in comments:
                    if c.get("isAnswer"):
                        if self.stats.mark_accepted(a["id"]):
                            accepted_count += 1
                            console.print(f"[green]Accepted: #{a['number']} {a['title']}[/green]")
                            logger.info(f"Accepted: repo={org}/{repo} #{a['number']}")
                            p = self.stats.badge_progress()
                            self.webhook.send_acceptance_notification(
                                title=a["title"], repo=f"{org}/{repo}",
                                total_accepted=p["accepted"], badge_tier=p["tier"],
                            )
                            break
                if idx < len(pending):
                    time.sleep(0.5)
            except Exception as e:
                console.print(f"[red]Error checking #{a['number']}: {e}[/red]")
                logger.error(f"Error checking accepted: {e}")

        if accepted_count:
            console.print(f"[green]{accepted_count} newly accepted![/green]")
            self.stats.display()
        else:
            console.print("[dim]No new acceptances[/dim]")
        return accepted_count

    def show_model_stats(self):
        rows = model_tracker.summary()
        if not rows:
            console.print("[dim]No model performance data yet[/dim]")
            return
        t = Table(title="Model Performance This Session", style="cyan")
        t.add_column("Model",       style="bold white")
        t.add_column("Successes",   style="green")
        t.add_column("Failures",    style="red")
        t.add_column("Avg Latency", style="yellow")
        for model, wins, losses, avg_lat in rows[:10]:
            short = model.split("/")[-1]
            t.add_row(short, str(wins), str(losses), f"{avg_lat:.1f}s")
        console.print(t)

    def run(self):
        console.print(Panel.fit(
            "[bold green]Galaxy Brain Badge Bot v6[/bold green]\n"
            f"[dim]User: {self.github_username}[/dim]\n"
            "[dim]Auto-discovery | Newest first | CoC-aware | Comment-aware | "
            "Circuit breakers | Adaptive rate limiting | Smart model selection[/dim]",
            border_style="green",
        ))

        self.stats.display()
        self.stats.display_by_org()

        if not self.key_manager.openrouter_keys:
            console.print("[red]No OPENROUTER_KEYS in .env[/red]")
            return

        console.print("\n[bold cyan]Configuration:[/bold cyan]")
        console.print(f"  Max answers/session : {self.max_answers}")
        console.print(f"  Delay between posts : {self.delay}s")
        console.print(f"  Auto-post           : {'ON' if self.auto_post else 'OFF'}")
        console.print(f"  Discovery topics    : {', '.join(self.discovery_topics[:5])}")
        console.print(f"  Discovery min stars : {self.discovery_min_stars}")
        console.print(f"  OpenRouter keys     : {len(self.key_manager.openrouter_keys)}")
        console.print(f"  Models              : {len(MODELS)}")
        console.print(f"  Cache TTL           : {CACHE_TTL_SECONDS}s")
        console.print(f"  Circuit breakers    : ON (threshold={CIRCUIT_BREAKER_THRESHOLD})")
        console.print(f"  Repo cooldown       : {REPO_COOLDOWN_MINUTES}min")
        console.print(f"  Staleness limit     : {STALENESS_DAYS}d")
        console.print(f"  Health server       : {'ON :' + str(HEALTH_CHECK_PORT) if HEALTH_CHECK_PORT else 'OFF'}")

        targets  = self._build_target_list()
        answered = self.find_and_answer(targets)

        p = self.stats.badge_progress()
        self.webhook.send_batch_summary(
            answered_count=answered,
            total_answers=p["total_answers"],
            acceptance_rate=p["acceptance_rate"],
            badge_tier=p["tier"],
        )

        console.print("\n[bold green]Session complete[/bold green]")
        self.stats.display()
        self.stats.display_by_org()
        self.show_model_stats()
        logger.info(
            f"Session complete: answered={answered} total={p['total_answers']} accepted={p['accepted']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Galaxy Brain Badge Bot v6 — circuit breakers, adaptive rate limiting, smart model selection"
    )
    parser.add_argument("--check",       action="store_true",
                        help="Check for accepted answers only (does NOT run the main bot loop)")
    parser.add_argument("--stats",       action="store_true", help="Display stats and exit")
    parser.add_argument("--models",      action="store_true", help="Show model performance stats")
    parser.add_argument("--test",        action="store_true", help="Test mode: show answers but don't post")
    parser.add_argument("--verbose",     action="store_true",
                        help="Verbose mode: print full prompt and raw model output before post-processing")
    parser.add_argument("--topics",      type=str, default=None,
                        help="Override discovery topics (comma-separated)")
    parser.add_argument("--min-stars",   type=int, default=None,
                        help="Override minimum stars for discovery")
    parser.add_argument("--max-repos",   type=int, default=None,
                        help="Override max repos to discover")
    parser.add_argument("--cache-clear", action="store_true", help="Clear in-memory cache at startup")
    args = parser.parse_args()

    bot = GalaxyBrainBot()
    bot.verbose = getattr(args, "verbose", False)

    if args.cache_clear:
        cache.clear()  # Use the new clear() method — thread-safe, no direct _store access
        console.print("[yellow]Cache cleared[/yellow]")
    if args.topics:
        bot.discovery_topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    if args.min_stars is not None:
        bot.discovery_min_stars = args.min_stars
    if args.max_repos is not None:
        bot.discovery_max_repos = args.max_repos

    if args.stats:
        bot.stats.display()
        bot.stats.display_by_org()
    elif args.models:
        bot.show_model_stats()
    elif args.check:
        console.print("[bold cyan]--check mode: checking accepted answers only (no posting)[/bold cyan]")
        bot.check_accepted()
    elif args.test:
        console.print("[yellow]TEST MODE — no answers will be posted[/yellow]")
        bot.auto_post = False
        targets = bot._build_target_list()
        bot.find_and_answer(targets)
    else:
        try:
            bot.run()
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]Fatal: {e}[/red]")
            logger.exception("Fatal error")
            sys.exit(1)


if __name__ == "__main__":
    main()
