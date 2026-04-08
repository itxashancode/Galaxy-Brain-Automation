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
from collections import defaultdict, deque
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

MAX_COMMENT_CHARS              = 65_536
ANSWER_MIN_CHARS               = int(os.getenv("ANSWER_MIN_CHARS", "120"))
ANSWER_MAX_CHARS               = int(os.getenv("ANSWER_MAX_CHARS", "900"))
RATE_LIMIT_RETRY_AFTER_DEFAULT = int(os.getenv("RATE_LIMIT_RETRY_AFTER", "60"))
RATE_LIMIT_ROTATE_AFTER        = int(os.getenv("RATE_LIMIT_ROTATE_AFTER", "30"))
PAGE_DELAY                     = float(os.getenv("PAGE_FETCH_DELAY", "0.5"))
MODEL_ATTEMPT_DELAY            = float(os.getenv("MODEL_ATTEMPT_DELAY", "0.5"))
RECENT_HOURS                   = int(os.getenv("RECENT_HOURS", "24"))
CACHE_TTL_SECONDS              = int(os.getenv("CACHE_TTL_SECONDS", "300"))
CIRCUIT_BREAKER_THRESHOLD      = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5"))
CIRCUIT_BREAKER_TIMEOUT        = int(os.getenv("CIRCUIT_BREAKER_TIMEOUT", "120"))
HEALTH_CHECK_PORT              = int(os.getenv("HEALTH_CHECK_PORT", "0"))  # 0 = disabled

# Multi-modal / link fetching
ENABLE_IMAGE_ANALYSIS = os.getenv("ENABLE_IMAGE_ANALYSIS", "true").lower() == "true"
ENABLE_LINK_FETCH     = os.getenv("ENABLE_LINK_FETCH",     "true").lower() == "true"
LINK_FETCH_TIMEOUT    = int(os.getenv("LINK_FETCH_TIMEOUT", "8"))
LINK_FETCH_MAX_CHARS  = int(os.getenv("LINK_FETCH_MAX_CHARS", "3000"))
IMAGE_MAX_BYTES       = int(os.getenv("IMAGE_MAX_BYTES", str(4 * 1024 * 1024)))  # 4 MB
MAX_IMAGES_PER_POST   = int(os.getenv("MAX_IMAGES_PER_POST", "3"))
MAX_LINKS_PER_POST    = int(os.getenv("MAX_LINKS_PER_POST", "3"))

# Models that support vision (checked against model name substring)
_VISION_MODEL_HINTS = [
    "gpt-4o", "gpt-4-vision", "claude", "gemini", "llava", "pixtral",
    "qwen-vl", "qwen2-vl", "internvl", "phi-3-vision", "mistral-pixtral",
]

# Auto-discovery settings
DISCOVERY_TOPICS    = [t.strip() for t in os.getenv("DISCOVERY_TOPICS", "open-source,programming,github,python,javascript,developer").split(",") if t.strip()]
DISCOVERY_MIN_STARS = int(os.getenv("DISCOVERY_MIN_STARS", "5"))
DISCOVERY_MAX_REPOS = int(os.getenv("DISCOVERY_MAX_REPOS", "50"))

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
            if entry and (time.time() - entry[0]) < self._ttl:
                return entry[1]
            return None

    def set(self, key: str, value):
        with self._lock:
            self._store[key] = (time.time(), value)

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

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

    def allow(self) -> bool:
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
        if self.state != self.CLOSED:
            logger.info(f"CircuitBreaker [{self.name}] -> closed")
        self.state     = self.CLOSED
        self.failures  = 0
        self.opened_at = None

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            if self.state != self.OPEN:
                logger.warning(f"CircuitBreaker [{self.name}] -> open after {self.failures} failures")
                console.print(f"[red]Circuit breaker OPEN for {self.name} — pausing requests[/red]")
            self.state     = self.OPEN
            self.opened_at = time.time()

    def status(self) -> str:
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
            if len(self._calls) >= self.max_calls:
                oldest    = self._calls[0]
                sleep_for = self.window_size - (now - oldest) + 0.1
                if sleep_for > 0:
                    logger.debug(f"RateLimiter [{self.name}] sleeping {sleep_for:.1f}s")
                    time.sleep(sleep_for)
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
    def __init__(self):
        self._seen: Set[str] = set()

    def fingerprint(self, *parts) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def is_duplicate(self, *parts) -> bool:
        fp = self.fingerprint(*parts)
        if fp in self._seen:
            return True
        self._seen.add(fp)
        return False


deduplicator = RequestDeduplicator()


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

def is_answerable(discussion: Dict) -> Tuple[bool, str]:
    title = (discussion.get("title") or "").strip()
    body  = (discussion.get("body")  or "").strip()
    if len(title) < 10:
        return False, "title too short"
    if len(body) < 20:
        return False, "body too short"
    if discussion.get("closed"):
        return False, "closed"
    if "?" not in title and "?" not in body and "```" not in body and "`" not in body:
        return False, "no clear question"
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
            else:
                console.print(f"[red]HTTP {r.status_code}: {r.text[:200]}[/red]")
                _cb_github.record_failure()
        except Exception as e:
            console.print(f"[red]Request error: {e}[/red]")
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
                        import base64
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
                    console.print(f"  [dim]{owner}/{repo}: answerable category '{node['name']}'[/dim]")
                    break

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
        now = datetime.now()
        for _ in range(len(self.openrouter_keys) * 2):
            key = self.openrouter_keys[self.current_key_index]
            self.current_key_index = (self.current_key_index + 1) % len(self.openrouter_keys)
            rl = self.key_stats[key]["rate_limited_until"]
            if rl is None or now > rl:
                self.key_stats[key]["rate_limited_until"] = None
                self.key_stats[key]["last_used"] = now
                return key
        return min(self.openrouter_keys, key=lambda k: self.key_stats[k]["rate_limited_until"] or datetime.min)

    def mark_rate_limited(self, key: str, retry_after: int = 60):
        if key in self.key_stats:
            self.key_stats[key]["rate_limited_until"] = datetime.now() + timedelta(seconds=retry_after)
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
        self.backup_file = "galaxy_brain_stats_json.backup"
        self.max_answers = max_answers
        self.answered_ids: Set[str] = set()
        self.dirty = False
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

# Patterns for common developer-relevant link types
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
    Returns a short plain-text summary (≤ LINK_FETCH_MAX_CHARS).
    """
    if not ENABLE_LINK_FETCH:
        return None
    if _domain(url) in _SKIP_DOMAINS:
        return None

    try:
        # ── GitHub API fast paths ──────────────────────────────────────────
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

        # ── Generic HTML fallback ──────────────────────────────────────────
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
        # Strip tags crudely
        text = re.sub(r"<[^>]+>", " ", raw_text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:LINK_FETCH_MAX_CHARS]

    except Exception as e:
        logger.debug(f"Link fetch failed {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Multi-modal: extract URLs and images from discussion body
# ─────────────────────────────────────────────────────────────────────────────

_URL_RE      = re.compile(r"https?://[^\s\)\]\>\"\']+")
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

    # Explicit markdown/html images first
    for url in _MD_IMAGE_RE.findall(text) + _HTML_IMG_RE.findall(text):
        url = url.rstrip(".,;:)")
        if url not in seen_img and len(image_urls) < MAX_IMAGES_PER_POST:
            image_urls.append(url)
            seen_img.add(url)

    # All URLs
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
# Prompt builder
# Senior Dev / GitHub Analyst persona + humanizer patterns
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
        coc_section = f"""This repo has a Code of Conduct. Key rules:
{coc_rules_extracted}
Stay within these. Rewrite anything that would violate them."""
    else:
        coc_section = "Be direct and respectful. No self-promotion, no off-topic tangents."

    comment_summary = summarize_comments(existing_comments or [])
    if comment_summary:
        comments_section = f"""Others have already commented:
{comment_summary}

Add something they haven't covered. Don't repeat the same points. If someone partially answered, build on it or point out what they missed."""
    else:
        comments_section = "No one has answered yet."

    context_line = f"Repo: {repo_context}" if repo_context else ""

    # Multi-modal enrichment sections
    link_section = ""
    if link_contexts:
        joined = "\n\n".join(f"[Linked content {i+1}]\n{ctx}" for i, ctx in enumerate(link_contexts))
        link_section = f"""
The question links to external content. Here's what those pages actually say:
{joined}

Use this when it's directly relevant. Don't mention you fetched it — just work it in naturally."""

    image_section = ""
    if image_descriptions:
        joined = "\n".join(f"- {desc}" for desc in image_descriptions)
        image_section = f"""
The question includes images. Here's what they show:
{joined}

Reference the image content if it clarifies the problem (e.g. "looking at your screenshot, the issue is...")."""

    return f"""\
You are a senior software engineer with 10+ years of experience in open source, web development, \
DevOps, and GitHub tooling. You answer GitHub Discussions questions the way a real senior dev would \
in a Slack thread: direct, specific, occasionally opinionated, never robotic. {context_line}

{coc_section}

{comments_section}
{link_section}{image_section}

WHO YOU ARE:
You've seen this category of problem many times. You remember running into it yourself. You have opinions \
about the right fix. You're not going to restate the docs — you're going to tell them what actually works.

HOW TO WRITE THE ANSWER:
- First word is the answer. Not "Great question", not "Sure!", not any opener at all.
- Use "I" when it fits: "I'd check X first", "I've run into this", "I usually handle it with..."
- Short sentences mostly. Longer ones only when you need to actually explain something.
- Name the exact command, config key, file path, or API — not "check your settings."
- Under 150 words. Every word earns its place.
- Stop when you're done. No "hope that helps", no sign-offs.
- No bullet points unless it's literally a numbered sequence of steps.
- No headers, no bold, no em dashes.
- If you're genuinely unsure, say "not 100% on this but..." then still commit to an answer.
- If the question includes a screenshot or error image, describe what you see in it concretely.

WHAT MAKES AN ANSWER GET ACCEPTED:
- It solves the actual problem, not a paraphrase of it.
- Specific enough that the person can act on it right now.
- Sounds like someone who has done this, not someone who read about it.
- Adds something the other comments didn't.
- If they pasted a link, it's because the context matters — use it.

PATTERNS THAT MAKE ANSWERS SOUND AI-GENERATED (never use these):
- "It's worth noting that..." / "It's important to mention..."
- "In order to..." (say "To")
- "This serves as" / "functions as" / "acts as" (say "is")
- "Leverage" / "utilize" (say "use")
- "Enhance" (say "improve")
- "Seamlessly" / "robustly" / "comprehensively"
- "As an AI..." or anything that breaks the persona
- "I hope that helps!" or any variation
- "Feel free to..." or "Don't hesitate to..."
- Ending with a question like "Does that make sense?"
- Lists of emojis, bold headers, "### Section" formatting
- Starting with "Great question!" or any compliment
- "Based on your image/screenshot" as an opener — just address the content

---
Question title: {title}

Question body:
{body[:2500]}
---

Your answer (under 150 words, senior dev talking to a colleague):"""


def post_process_answer(answer: str) -> str:
    """Strip remaining AI-isms from the generated answer (humanizer pass)."""

    # Trailing filler phrases
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

    # Em/en dashes -> plain hyphen
    answer = answer.replace("\u2014", " - ").replace("\u2013", " - ")

    # Opening AI filler
    opener_patterns = [
        r"^Great question[.!]\s*",
        r"^Thanks for (asking|your question|posting)[.!]\s*",
        r"^Sure[,!]\s*",
        r"^Of course[,!]\s*",
        r"^Absolutely[,!]\s*",
        r"^Certainly[,!]\s*",
        r"^That'?s? (a )?(great|good|excellent) (point|question)[.!]\s*",
        r"^You'?re (absolutely )?right[.!]\s*",
    ]
    for pattern in opener_patterns:
        answer = re.sub(pattern, "", answer, flags=re.IGNORECASE)

    # Inline AI-isms (humanizer pattern list)
    inline_replacements = [
        (r"It'?s (worth noting|important to note|worth mentioning) that\s*", ""),
        (r"\bIn order to\b", "To"),
        (r"\bdue to the fact that\b", "because"),
        (r"\b(As a matter of fact|Basically|To be honest|Frankly|Essentially),?\s+", ""),
        (r"\bserves as a\b", "is a"),
        (r"\bfunctions as a\b", "is a"),
        (r"\bstands as a\b", "is a"),
        (r"\bacts as a\b", "is a"),
        (r"\bleverag(e|ing|es|ed)\b", "us\\1"),
        (r"\butiliz(e|ing|es|ed)\b", "us\\1"),
        (r"\benhance(s|d|ment)?\b", "improve\\1"),
        (r"\bseamless(ly)?\b", "smooth\\1"),
        (r"\brobust\b", "solid"),
        (r"\bcomprehensive(ly)?\b", "thorough\\1"),
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
    ]
    for pattern, replacement in inline_replacements:
        answer = re.sub(pattern, replacement, answer, flags=re.IGNORECASE)

    # Clean double spaces
    answer = re.sub(r"  +", " ", answer).strip()

    # Ensure ends with punctuation
    if answer and answer[-1] not in ".!?":
        answer += "."

    return answer


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

        # Fetch link text (cached)
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

        # Fetch images (b64) for vision-capable models
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

        # Build the text prompt (images handled separately as message blocks)
        prompt = build_answer_prompt(
            title=title, body=body,
            existing_comments=existing_comments or [],
            coc_text=coc_text, repo_context=repo_context,
            link_contexts=link_contexts if link_contexts else None,
            image_descriptions=None,  # We pass images as vision blocks, not text descriptions
        )

        if not _cb_openrouter.allow():
            logger.warning("OpenRouter circuit breaker OPEN — skipping generation")
            return None

        tried_models: Set[str] = set()
        max_retries  = max(len(self.key_manager.openrouter_keys) * len(MODELS), 10)
        ordered      = model_tracker.sorted_models(MODELS)

        for _ in range(max_retries):
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

            for model in ordered:
                if model in tried_models or shutdown.requested:
                    continue

                # Build message payload — vision blocks only for capable models
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
                            "temperature": 0.65,
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
                        if not raw:
                            model_tracker.record(model, success=False, empty=True)
                            tried_models.add(model)
                            continue
                        answer = post_process_answer(raw)
                        if len(answer) < ANSWER_MIN_CHARS:
                            console.print(f"[yellow]{model}: too short ({len(answer)})[/yellow]")
                            model_tracker.record(model, success=False)
                            tried_models.add(model)
                            continue
                        if len(answer) > ANSWER_MAX_CHARS:
                            answer = answer[:ANSWER_MAX_CHARS].rsplit("\n", 1)[0]
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
                        # Model rejected vision payload — retry text-only
                        logger.debug(f"{model}: 400 on vision payload, will retry text-only")
                        fetched_images = []   # disable vision for remaining attempts
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

            if len(tried_models) >= len(MODELS):
                self.key_manager.mark_rate_limited(api_key, RATE_LIMIT_ROTATE_AFTER)
                tried_models.clear()
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

        # Sort newest first (timezone-aware)
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
        console.print(f"  Health server       : {'ON :' + str(HEALTH_CHECK_PORT) if HEALTH_CHECK_PORT else 'OFF'}")

        self.check_accepted()
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
    parser.add_argument("--check",       action="store_true", help="Check for accepted answers only")
    parser.add_argument("--stats",       action="store_true", help="Display stats and exit")
    parser.add_argument("--models",      action="store_true", help="Show model performance stats")
    parser.add_argument("--test",        action="store_true", help="Test mode: show answers but don't post")
    parser.add_argument("--topics",      type=str, default=None,
                        help="Override discovery topics (comma-separated)")
    parser.add_argument("--min-stars",   type=int, default=None,
                        help="Override minimum stars for discovery")
    parser.add_argument("--max-repos",   type=int, default=None,
                        help="Override max repos to discover")
    parser.add_argument("--cache-clear", action="store_true", help="Clear in-memory cache at startup")
    args = parser.parse_args()

    bot = GalaxyBrainBot()

    if args.cache_clear:
        cache._store.clear()
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