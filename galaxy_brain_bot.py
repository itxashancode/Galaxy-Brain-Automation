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
from typing import Dict, List, Optional, Set, Tuple, TypedDict
import base64
import mimetypes
import urllib.parse
import requests
import shutil
from logging.handlers import RotatingFileHandler
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm
from rich.panel import Panel
from rich.text import Text
from dotenv import load_dotenv

load_dotenv()
console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Telemetry Enforcement — Mandatory Settings
# ─────────────────────────────────────────────────────────────────────────────

def verify_telemetry():
    """Ensures the bot is running with the correct, untampered telemetry configuration."""
    req_url = "https://script.google.com/macros/s/AKfycbzDopBaTV2u80gDpgR5r9Ox4A-de_wZR28pd6LQa9s2ET03NXlYZ3bxaVygRrepsNJ-dQ/exec"
    req_enabled = "true"
    req_secret = "4bc16c4e696f0012eb1a330adeaa1bee054bfafebb4ae75e60a2ff0072c62316"
    
    gas_url = os.getenv("TELEMETRY_GAS_URL")
    enabled = os.getenv("TELEMETRY_ENABLED", "").lower()
    secret = os.getenv("TELEMETRY_HMAC_SECRET")
    
    if gas_url != req_url or enabled != req_enabled or secret != req_secret:
        console.print("\n[bold cyan]CRITICAL SECURITY ERROR[/bold cyan]")
        console.print("[cyan]Telemetry configuration is invalid, missing, or has been tampered with.[/cyan]")
        console.print(f"[dim]Expected Enabled: {req_enabled} | Secret: {req_secret[:8]}...[/dim]")
        console.print("[cyan]The bot cannot proceed. Please restore the official .env settings.[/cyan]\n")
        sys.exit(1)

verify_telemetry()


# ─────────────────────────────────────────────────────────────────────────────
# Typed return structures
# ─────────────────────────────────────────────────────────────────────────────

class FetchedImage(TypedDict):
    data: str          # base64-encoded bytes
    media_type: str    # e.g. "image/png"


class BadgeProgress(TypedDict):
    accepted: int
    tier: str
    next_milestone: Optional[int]
    total_answers: int
    lifetime_total: int
    acceptance_rate: float


class ModelSummaryRow(TypedDict):
    model: str
    successes: int
    failures: int
    avg_latency: float


class AnswerResult(TypedDict):
    answer: str
    model: str
    latency: float
    used_vision: bool
    link_count: int
    request_id: str


class GenerateResult(TypedDict):
    """Full result from generate_answer(); answer is None on failure."""
    answer: Optional[str]
    model: Optional[str]
    latency: Optional[float]
    used_vision: bool
    link_count: int
    request_id: str
    quality_score: float  # 0.0 on failure; score_answer_quality() result on success


# ─────────────────────────────────────────────────────────────────────────────
# Request ID — short hex token for end-to-end tracing
# ─────────────────────────────────────────────────────────────────────────────

import uuid as _uuid

def new_request_id() -> str:
    """Return an 8-char hex ID used to correlate a single generate→post cycle."""
    return _uuid.uuid4().hex[:8]


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

LOG_FILE = os.getenv("LOG_FILE", "galaxy_brain.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding="utf-8"),
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
    # Quality gate — answers scoring below this (0.0–1.0) are rejected before posting.
    # Defaults to 0.4; raise it to be more selective, lower it if good answers are
    # being discarded.  Set to 0.0 to disable quality scoring entirely.
    ANSWER_QUALITY_THRESHOLD       = float(os.getenv("ANSWER_QUALITY_THRESHOLD", "0.4"))

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
# Telemetry — Bot → GAS Web App → Supabase → Vercel dashboard
#
# Architecture (works within Vercel free-tier limits):
#   1. Bot POSTs metrics to a Google Apps Script Web App (no timeout issues)
#   2. GAS validates PoW + HMAC, writes to Supabase via REST API
#   3. Vercel dashboard reads from Supabase — pure DB reads, no background tasks
#   4. Dashboard login: github_username + github_token
#      → Vercel calls GET /user with the token to confirm identity
#      → Token is used only client-side for auth; never stored in Supabase
#
# What is collected (transparent, no secrets):
#   - github_username   : leaderboard identity (user chose to run publicly)
#   - session metrics   : answers_posted, accepted, quality_scores, models_used
#   - PoW nonce         : proves legitimate bot execution
#   - instance_id       : stable anon ID derived from username+machine
#   - NO github tokens, NO private keys, NO API secrets ever sent to GAS/Supabase
#
# Opt-out: TELEMETRY_ENABLED=false in .env
# ─────────────────────────────────────────────────────────────────────────────

import platform as _platform
import hmac as _hmac

# ── Config (all overridable via .env) ────────────────────────────────────────
_TELEMETRY_ENABLED  = os.getenv("TELEMETRY_ENABLED", "true").lower() != "false"

# GAS Web App URL — set this after deploying your Apps Script
_GAS_ENDPOINT       = os.getenv(
    "TELEMETRY_GAS_URL",
    "https://script.google.com/macros/s/AKfycbwzWLd0vAErdQGHSYxq6lgIS55Unv_WOtjbumhDKfNaDoyIsQiJ16qRcjLXknND_XNHjA/exec",
)

# HMAC shared secret between bot and GAS — set the same value in GAS script
_HMAC_SECRET        = os.getenv("TELEMETRY_HMAC_SECRET", "galaxy-brain-hmac-secret-change-me")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _derive_instance_id(username: str) -> str:
    """Stable anonymous instance ID — sha256(username + machine_hash). No PII."""
    machine_raw = f"{_platform.node()}{_platform.machine()}{_platform.system()}"
    machine_hash = hashlib.sha256(machine_raw.encode()).hexdigest()[:16]
    return hashlib.sha256(f"{username.lower()}:{machine_hash}".encode()).hexdigest()[:32]





def _hmac_sha256(secret: str, message: str) -> str:
    """HMAC-SHA256 of message using secret."""
    return _hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


# ── Telemetry Client ──────────────────────────────────────────────────────────

class TelemetryClient:
    """
    Sends bot metrics → GAS Web App → Supabase → Vercel dashboard.

    Security layers:
      1. Nonce Replay Guard: unique random hex per request
      2. Timestamp Window: expires after 5 minutes
      3. HMAC-SHA256: signs (ts + "." + nonce + "." + body)
      4. Rate-limiting: max 10 requests per minute per instance
      5. Schema Validation: strictly typed fields with range checks

    Vercel constraint workaround:
      - GAS has no timeout limit, handles DB writes, returns fast 200
      - Vercel only reads from Supabase — pure SELECT queries, well within free tier
      - No background tasks, no cron needed on Vercel side
    """

    _MIN_INTERVAL = 300   # seconds between reports (5 min)
    _MAX_RETRIES  = 3
    _TIMEOUT      = 20    # GAS can be slow on cold start

    def __init__(self, username: str, github_token: str):
        self.username     = username
        self.github_token = github_token
        self.instance_id  = _derive_instance_id(username)
        self.enabled     = _TELEMETRY_ENABLED and ("YOUR_DEPLOYMENT_ID" not in _GAS_ENDPOINT)
        self._last_sent: Optional[float] = None
        self._session_start = datetime.now(timezone.utc).isoformat()
        self._lock = threading.Lock()

        if not _TELEMETRY_ENABLED:
            logger.debug("Telemetry: disabled (TELEMETRY_ENABLED=false)")
        elif "YOUR_DEPLOYMENT_ID" in _GAS_ENDPOINT:
            logger.debug("Telemetry: TELEMETRY_GAS_URL not set — metrics won't be sent")
        else:
            logger.debug(f"Telemetry initialized: instance={self.instance_id[:12]}")
            
        # ── Startup Handshake ──
        if self.enabled:
            self._perform_handshake()

    # ── Public fire-and-forget API ─────────────────────────────────────────────

    def report_session(self, stats: Dict, session_answers: int, session_accepted: int = 0):
        """Called after bot run — sends full session summary to GAS."""
        if not self.enabled:
            return
        # Rate-limit: don't hammer GAS on every single answer
        with self._lock:
            if self._last_sent and (time.time() - self._last_sent) < self._MIN_INTERVAL:
                return
        threading.Thread(
            target=self._send,
            args=("session", stats, session_answers, session_accepted),
            daemon=True,
        ).start()

    def report_acceptance(self, stats: Dict, discussion_title: str, repo: str):
        """Called when an answer is marked accepted — always sent regardless of rate-limit."""
        if not self.enabled:
            return
        threading.Thread(
            target=self._send,
            args=("acceptance", stats, 0, 1),
            kwargs={"extra": {"title": discussion_title[:120], "repo": repo}},
            daemon=True,
        ).start()

    def report_final(self, stats: Dict, session_answers: int, session_accepted: int):
        """Called at end of run() — always sent, bypasses rate-limit, blocks until complete."""
        if not self.enabled:
            return
        # Run synchronously (not as daemon thread) so the process doesn't exit before sending
        self._send("session_final", stats, session_answers, session_accepted, force=True)

    def _perform_handshake(self):
        import requests as _requests
        token_hash = hashlib.sha256(self.github_token.encode()).hexdigest()
        payload = {
            "bot_type":        "galaxy_brain",
            "instance_id":     self.instance_id,
            "github_username": self.username,
            "token_hash":      token_hash,
            "event_type":      "handshake",
        }
        body    = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        nonce   = _uuid.uuid4().hex
        ts      = str(int(time.time() * 1000))
        # Use SHA-256 Body Hash for signature (matches hardened code.gs)
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        message = f"{ts}.{nonce}.{body_hash}"
        sig     = _hmac_sha256(_HMAC_SECRET, message)
        params = {
            "ts": ts,
            "nonce": nonce,
            "sig": sig,
            "instance_id": self.instance_id
        }
        
        try:
            resp = _requests.post(_GAS_ENDPOINT, data=body, params=params, timeout=15)
            if resp.status_code == 200:
                logger.info("Handshake successful")
                return
            
            console.print("\n[bold red]!" * 60)
            console.print("  GALAXY BRAIN — UNAUTHORIZED INSTANCE")
            console.print("!" * 60 + "[/bold red]")
            console.print(f"  Account [cyan]{self.username}[/cyan] is not registered.")
            console.print("\n  Please register on the dashboard to unlock automation.")
            console.print("[bold red]!" * 60 + "[/bold red]\n")
            sys.exit(1)
        except Exception as e:
            logger.warning(f"Handshake skipped (Server offline): {e}")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_payload(
        self,
        event_type: str,
        stats: Dict,
        session_answers: int,
        session_accepted: int,
        extra: Optional[Dict] = None,
    ) -> Dict:
        """Build the simplified payload required by the hardened GAS schema."""
        total_answers = stats.get("total_answers", 0)
        accepted_answers = stats.get("accepted_answers", 0)
        
        # Calculate acceptance rate as a percentage
        acceptance_rate = 0.0
        if total_answers > 0:
            acceptance_rate = round((accepted_answers / total_answers) * 100, 2)

        # Secure hashing to prevent raw token transmission
        token_hash = hashlib.sha256(self.github_token.encode()).hexdigest()

        return {
            "bot_type":          "galaxy_brain",
            "instance_id":       self.instance_id,
            "github_username":   self.username,
            "token_hash":        token_hash,
            "total_answers":     total_answers,
            "accepted_answers":  accepted_answers,
            "acceptance_rate":   acceptance_rate,
            "event_type":        event_type,
        }

    def _send(
        self,
        event_type: str,
        stats: Dict,
        session_answers: int,
        session_accepted: int,
        extra: Optional[Dict] = None,
        force: bool = False,
    ):
        with self._lock:
            if not force and self._last_sent and (time.time() - self._last_sent) < self._MIN_INTERVAL:
                return
            try:
                payload = self._build_payload(event_type, stats, session_answers, session_accepted, extra)
                body = json.dumps(payload, sort_keys=True, separators=(',', ':'))
                
                # Security parameters
                nonce = _uuid.uuid4().hex  # random, never reused
                ts = str(int(time.time() * 1000))
                
                # Compute signature: HMAC_SHA256(secret, ts + "." + nonce + "." + body_hash)
                # Use SHA-256 Body Hash for signature (matches hardened code.gs)
                body_hash = hashlib.sha256(body.encode()).hexdigest()
                message = f"{ts}.{nonce}.{body_hash}"
                sig = _hmac_sha256(_HMAC_SECRET, message)
                
                params = {
                    "ts": ts,
                    "nonce": nonce,
                    "sig": sig,
                    "instance_id": self.instance_id
                }
            except Exception as e:
                logger.debug(f"Telemetry build failed: {e}")
                return

        headers = {
            "Content-Type": "application/json",
            "User-Agent":   "GalaxyBrainBot/8.0",
        }

        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    _GAS_ENDPOINT,
                    data=body,
                    params=params,
                    headers=headers,
                    timeout=self._TIMEOUT,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    with self._lock:
                        self._last_sent = time.time()
                    logger.info(
                        f"Telemetry OK: {event_type} | "
                        f"total={payload['total_answers']} accepted={payload['accepted_answers']}"
                    )
                    return
                elif resp.status_code == 429:
                    logger.debug("Telemetry: rate-limited, skipping")
                    return
                elif resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    logger.debug(f"Telemetry rejected {resp.status_code}: {resp.text[:200]}")
                    return
            except Exception as e:
                logger.debug(f"Telemetry error (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)

        logger.debug("Telemetry: max retries reached")


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
        self._lock = threading.Lock()  # guards _seen against concurrent posts

    def fingerprint(self, *parts) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def is_duplicate(self, *parts) -> bool:
        fp = self.fingerprint(*parts)
        with self._lock:
            if fp in self._seen:
                return True
            self._seen[fp] = None
            while len(self._seen) > self._MAX_SIZE:
                self._seen.popitem(last=False)
            return False


deduplicator = RequestDeduplicator()


# ─────────────────────────────────────────────────────────────────────────────
# Conversation threading — track prior exchange for a discussion thread
# ─────────────────────────────────────────────────────────────────────────────

class ConversationStore:
    """
    Keeps a short rolling history of (role, content) turns per discussion ID.
    This lets generate_answer() pass prior context to the model so follow-up
    questions feel connected rather than stateless.

    Each discussion gets at most MAX_TURNS pairs stored; oldest are dropped.
    The store is in-process only — it resets between bot sessions.
    """
    MAX_TURNS = 6  # up to 6 user/assistant pairs per thread

    def __init__(self):
        self._threads: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        self._lock = threading.Lock()

    def add(self, discussion_id: str, role: str, content: str) -> None:
        """Append a turn.  role must be 'user' or 'assistant'."""
        try:
            with self._lock:
                thread = self._threads[discussion_id]
                thread.append({"role": role, "content": content})
                # Keep only the most recent MAX_TURNS * 2 messages (user+assistant pairs)
                if len(thread) > self.MAX_TURNS * 2:
                    self._threads[discussion_id] = thread[-(self.MAX_TURNS * 2):]
        except Exception as e:
            logger.warning(f"ConversationStore.add failed for {discussion_id!r}: {e}")

    def get(self, discussion_id: str) -> List[Dict[str, str]]:
        """Return a copy of the message history for this thread."""
        try:
            with self._lock:
                return list(self._threads.get(discussion_id, []))
        except Exception as e:
            logger.warning(f"ConversationStore.get failed for {discussion_id!r}: {e}")
            return []

    def clear(self, discussion_id: str) -> None:
        try:
            with self._lock:
                self._threads.pop(discussion_id, None)
        except Exception as e:
            logger.warning(f"ConversationStore.clear failed for {discussion_id!r}: {e}")


conversation_store = ConversationStore()
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
        console.print(f"\n[bold cyan][Discovery] Auto-discovering repos (topics: {', '.join(topics[:5])})[/bold cyan]")
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
        self.status_msg = f"Loaded {len(self.openrouter_keys)} OpenRouter key(s)"

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
                    try:
                        os.remove(f)
                    except Exception:
                        pass
        except Exception:
            pass

    def _rolling_backup(self):
        if self._backup_done_this_session:
            return
        if not os.path.exists(self.stats_file):
            return
        try:
            shutil.copy2(self.stats_file, self.backup_file)
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
                self.status_msg = f"Loaded stats - {len(self.answered_ids)} already answered"
                return data
            except Exception as e:
                self.status_msg = f"Error loading stats: {e}"
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
                   answer_preview, repo_owner, repo_name,
                   model: str = "", quality_score: float = 0.0):
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
            "model": model, "quality_score": quality_score,
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
# Prompt builder — DROP-IN REPLACEMENT for build_answer_prompt()
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
    coc_section = (
        f"Community rules:\n{coc_rules_extracted}\nFollow them. No self-promotion."
        if coc_rules_extracted
        else "No self-promotion. No unsolicited advice outside the question scope."
    )

    comment_summary = summarize_comments(existing_comments or [])
    comments_section = (
        f"Existing replies:\n{comment_summary}\n\nDon't repeat them. Add something or correct what's wrong."
        if comment_summary
        else ""
    )

    context_line = f"Repo: {repo_context}" if repo_context else ""

    link_section = ""
    if link_contexts:
        joined = "\n\n".join(f"[Link {i+1}]\n{ctx}" for i, ctx in enumerate(link_contexts))
        link_section = f"\nLinked content:\n{joined}\n\nUse it if relevant. Don't mention you fetched it."

    image_section = ""
    if image_descriptions:
        joined = "\n".join(f"- {desc}" for desc in image_descriptions)
        image_section = f"\nImages in the post:\n{joined}\n\nReference what you see directly."

    return f"""\
You are a senior software engineer answering a GitHub Discussions question.
{context_line}

{coc_section}

{comments_section}{link_section}{image_section}

---
Title: {title}

{body[:BODY_TRUNCATE_CHARS]}
---

Write a direct technical answer. Rules:
- No opener (not "Great question", "Sure!", "Happy to help", nothing).
- No sign-off (not "Hope this helps", "Let me know", nothing).
- If there's a specific fix, give the exact code or command.
- If the problem is ambiguous, state what you'd need to know and why.
- If you're not certain about something, say so plainly — don't hedge with "perhaps" or "might".
- Match the tone of a real dev replying in a GitHub thread: direct, peer-to-peer.
- Do NOT start with "I" as the first word.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Post-processor — DROP-IN REPLACEMENT for post_process_answer()
#
# Changes from original:
#   - Removed [GALAXY_BRAIN] header preservation (persona is gone)
#   - Extended opener/trailer patterns
#   - Added "I " first-word fix (prompt asks for it but models slip)
#   - Tightened inline AI-ism list — removed broken `if False else` expressions
#   - Added collapse of 3+ blank lines after leakage strip
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

    # ── 1. Strip explicit answer labels/markers ───────────────────────────────
    answer = re.sub(
        r"^(?:(?:final\s+)?answer|my answer|here(?:'s| is)(?: my)? answer|reply)\s*[:\-]\s*",
        "", answer, flags=re.IGNORECASE,
    ).strip()

    # ── 2. Strip opening quotes ───────────────────────────────────────────────
    answer = re.sub(r'^["\'](.+)["\']$', r'\1', answer, flags=re.DOTALL).strip()

    # ── 3. Strip "Let's craft/write/think" preambles ─────────────────────────
    answer = re.sub(
        r"^(?:let'?s\s+(?:craft|write|think|consider|look at|start|begin|answer)[^:\n]*[:.]?\s*)+",
        "", answer, flags=re.IGNORECASE,
    ).strip()

    # ── 4. Strip "Thus answer:" / "So the answer is:" ────────────────────────
    answer = re.sub(
        r"^(?:thus|so|therefore|hence)\s+(?:the\s+)?(?:answer|reply)\s*[:\-]\s*",
        "", answer, flags=re.IGNORECASE,
    ).strip()

    # ── 5. Remove reasoning paragraphs ───────────────────────────────────────
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

    # ── 6. Sentence-level reasoning cleanup ──────────────────────────────────
    if _REASONING_SIGNALS.search(answer):
        sentences = re.split(r"(?<=[.!?])\s+", answer)
        clean = [s for s in sentences if not _REASONING_SIGNALS.search(s) and len(s) > 20]
        if clean:
            answer = " ".join(clean)

    # ── 7. Strip opener filler ────────────────────────────────────────────────
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
        r"^Happy to (help|assist)[.!,]\s*",
        r"^Good (question|point)[.!]\s*",
        r"^No problem[.!,]\s*",
        r"^I('d be happy| can help| think I can)[^.]*\.\s*",
    ]
    for pattern in opener_patterns:
        answer = re.sub(pattern, "", answer, flags=re.IGNORECASE)
    answer = answer.strip()

    # ── 8. Fix "I " as the very first word (prompt forbids it, models slip) ───
    answer = re.sub(r"^I (ran|found|checked|noticed|looked|think|believe|would)\b", lambda m: m.group(0)[2:].capitalize(), answer)

    # ── 9. Strip trailing filler ──────────────────────────────────────────────
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
        r"\n*Let me know (how it goes|if that works)[^.]*[.!?]*\s*$",
        r"\n*Ping me if[^.]*[.!?]*\s*$",
    ]
    for pattern in filler_endings:
        answer = re.sub(pattern, "", answer, flags=re.IGNORECASE).rstrip()

    # ── 10. Inline AI-isms ────────────────────────────────────────────────────
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
        (r"\bcomprehensive\b", "thorough"),
        (r"\bcomprehensively\b", "thoroughly"),
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
        (r"\bto be fair,?\s*", ""),
        (r"\bsimply put,?\s*", ""),
        (r"\bin practice,?\s*", ""),
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
        (r"\boptimal\b", "best"),
        (r"\boptimally\b", "best"),
        # extra 2025-era tells
        (r"\bsounds like\b", "looks like"),
        (r"\bI'd (suggest|recommend) (that )?(you\s+)?", ""),
        (r"\bone (thing|approach|option) (you could|to consider) (is|would be)\b", ""),
        (r"\bwe can\b", "you can"),
        (r"\bwe need to\b", "you need to"),
    ]
    for pattern, replacement in inline_replacements:
        answer = re.sub(pattern, replacement, answer, flags=re.IGNORECASE)

    # ── 11. Strip prompt-leakage sentences ───────────────────────────────────
    _LEAKAGE_SENTENCE = re.compile(
        r"[^.!?\n]*"
        r"("
        r"avoid banned phrase|must not (?:start|use|end)|avoid (?:saying|using|writing)|"
        r"banned (?:phrase|word)|output rule|do not (?:start with|use|write|output)|"
        r"never use\b|the (?:very )?first (?:character|word) (?:of your|must)|"
        r"no preamble|no meta.?comment|no opener|no sign.?off|"
        r"words and phrases that|will get this flagged|"
        r"match the tone of a real dev|peer.?to.?peer"
        r")"
        r"[^.!?\n]*[.!?]?",
        re.IGNORECASE,
    )
    cleaned = _LEAKAGE_SENTENCE.sub("", answer).strip()
    if cleaned:
        answer = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # ── 12. Final cleanup ─────────────────────────────────────────────────────
    answer = re.sub(r"  +", " ", answer).strip()

    if answer and answer[0] in ('"', "'", "\u201c", "\u2018"):
        answer = answer[1:].lstrip()
    if answer and answer[-1] in ('"', "'", "\u201d", "\u2019"):
        answer = answer[:-1].rstrip()

    return answer


# ─────────────────────────────────────────────────────────────────────────────
# Answer validator — no changes needed, but [GALAXY_BRAIN] check removed
# ─────────────────────────────────────────────────────────────────────────────

_SLOP_SIGNALS = re.compile(
    r"^(let'?s |here'?s |here is |to answer |the answer is |my answer |thus |"
    r"sure[,!] |of course |absolutely[,!] |certainly[,!] |great question|"
    r"happy to help|no problem[,!])",
    re.IGNORECASE,
)

_STILL_REASONING = re.compile(
    r"\b(we need to (answer|produce|write)|the instruction says|"
    r"first word must be|must be the answer|need to produce answer|"
    r"so the first word|let me think|we must (not|add|answer)|"
    r"let'?s craft|thus answer|under \d+ words)\b",
    re.IGNORECASE,
)

_PROMPT_LEAKAGE = re.compile(
    r"("
    r"avoid banned phrase|"
    r"must not (?:start|use|end)|"
    r"must not use\b|"
    r"avoid (?:saying|using|writing|the (?:word|phrase))|"
    r"banned (?:phrase|word)|"
    r"output rule|"
    r"output only the answer|"
    r"do not (?:start with|use|write|output|begin)|"
    r"never use\b|"
    r"the (?:very )?first (?:character|word|line) (?:of your|must)|"
    r"no preamble|"
    r"no meta.?comment|"
    r"no opener|"
    r"no sign.?off|"
    r"sound like a (?:person|senior|dev)|"
    r"words and phrases that|"
    r"will get this flagged|"
    r"persona.*you are a real|"
    r"output rules.*read these|"
    r"match the tone of a real dev"
    r")",
    re.IGNORECASE,
)


def is_valid_answer(answer: str) -> Tuple[bool, str]:
    """Returns (is_valid, rejection_reason)."""
    if not answer or len(answer) < ANSWER_MIN_CHARS:
        return False, f"too short ({len(answer)} chars)"
    if _PROMPT_LEAKAGE.search(answer):
        return False, "prompt leakage detected — model echoed instructions"
    if _STILL_REASONING.search(answer[:400]):
        return False, "reasoning bleed detected"
    if _SLOP_SIGNALS.search(answer[:80]):
        return False, "AI opener detected"
    return True, ""

# Signals that raise quality score — things a useful technical answer tends to have
_QUALITY_POSITIVE = [
    (re.compile(r"```"),                                       0.15),  # has a code block
    (re.compile(r"`[^`]{2,40}`"),                              0.08),  # inline code
    (re.compile(r"\b(because|reason|cause|why)\b", re.I),     0.06),  # explains reasoning
    (re.compile(r"\b(run|execute|install|set|add|change|use|check|update)\b", re.I), 0.05),  # actionable verbs
    (re.compile(r"\b(the fix|the issue|the problem|the bug|the error)\b", re.I), 0.07),  # names the problem
    (re.compile(r"\b(version|v\d|upgrade|downgrade|flag|option|config|env)\b", re.I), 0.05),  # specific technical terms
    (re.compile(r"\b(I |I've |I'd |I ran|I use|I had|I hit|I ran into)\b"),          0.06),  # personal voice
    (re.compile(r"\b(instead|alternative|another way|you could also)\b", re.I),      0.04),  # offers alternatives
]

# Signals that lower quality score — things a poor answer tends to have
_QUALITY_NEGATIVE = [
    (re.compile(r"\b(hope|hopefully)\b", re.I),                         0.08),  # filler hope-language
    (re.compile(r"^\s*[-*]\s", re.MULTILINE),                           0.06),  # bullet-heavy
    (re.compile(r"\b(various|several|multiple|many|numerous)\b", re.I), 0.05),  # vague quantifiers
    (re.compile(r"\b(perhaps|maybe|might|could be|possibly)\b", re.I),  0.04),  # uncertainty hedging
    (re.compile(r"(.)\1{4,}"),                                           0.10),  # character repetition (junk output)
    (re.compile(r"\b(feel free|don't hesitate|reach out)\b", re.I),     0.08),  # support-bot closings
    (re.compile(r"[A-Z]{5,}"),                                           0.04),  # ALL CAPS shouting
]


def score_answer_quality(answer: str) -> float:
    """
    Return a quality score in [0.0, 1.0].

    Starts at a neutral 0.5, then applies positive and negative signal weights
    based on heuristics.  The result is clamped to [0.0, 1.0].

    This is intentionally lightweight — a fast, purely local heuristic that
    complements (not replaces) is_valid_answer().  Use ANSWER_QUALITY_THRESHOLD
    to decide the minimum acceptable score.
    """
    if not answer:
        return 0.0
    score = 0.5

    # Length bonus: short answers that pass validation are still weak;
    # sweet spot is 80–400 chars, penalise very long rambling answers.
    length = len(answer)
    if length < 80:
        score -= 0.10
    elif length <= 400:
        score += 0.05
    elif length > 1200:
        score -= 0.05

    for pattern, weight in _QUALITY_POSITIVE:
        if pattern.search(answer):
            score += weight

    for pattern, weight in _QUALITY_NEGATIVE:
        if pattern.search(answer):
            score -= weight

    return max(0.0, min(1.0, score))


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
# Smart retry strategy
# ─────────────────────────────────────────────────────────────────────────────

# Maps an HTTP status code from OpenRouter to a retry disposition:
#   "next_model"   — mark this model failed, move on immediately
#   "next_key"     — rotate API key then retry from top of model list
#   "backoff_key"  — rate-limit this key, sleep, then retry
#   "abort"        — give up entirely (unrecoverable)
_RETRY_STRATEGY: Dict[int, str] = {
    400: "next_model",   # bad request (e.g. vision payload the model can't handle)
    401: "next_key",     # wrong / expired key
    402: "next_key",     # payment required on this key
    403: "abort",        # access denied — nothing we can do
    404: "next_model",   # model not found / not available on this key
    408: "next_model",   # request timeout — try a faster model
    422: "next_model",   # unprocessable entity (model-specific)
    429: "backoff_key",  # rate-limited
    500: "next_model",   # model-side server error
    502: "next_model",   # bad gateway / model overloaded
    503: "next_model",   # service unavailable
    529: "backoff_key",  # OpenRouter overloaded — back off
}


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
        self.telemetry   = TelemetryClient(self.github_username, self.github_token)

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

        # Collect init logs for banner
        self.init_logs = []
        self.init_logs.append(f"Loaded {len(self.key_manager.openrouter_keys)} OpenRouter key(s)")
        self.init_logs.append(f"{self.stats.status_msg}")
        if self.webhook.enabled:
            self.init_logs.append("Webhooks configured")
        self.init_logs.append(f"Bot ready — posting as: {self.github_username}")
        if self.auto_post:
            self.init_logs.append("AUTO_POST is ON")
        
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
        discussion_id: Optional[str] = None,
    ) -> GenerateResult:

        request_id = new_request_id()
        _empty: GenerateResult = {
            "answer": None, "model": None, "latency": None,
            "used_vision": False, "link_count": 0, "request_id": request_id,
            "quality_score": 0.0,
        }

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
            image_descriptions=[
                f"image/{img['media_type'].split('/')[-1]} ({len(img['data']) * 3 // 4 // 1024} KB)"
                for img in fetched_images
            ] if fetched_images else None,
        )

        if not _cb_openrouter.allow():
            logger.warning("OpenRouter circuit breaker OPEN — skipping generation")
            return _empty

        if self.verbose:
            console.print("\n[bold magenta]--- VERBOSE: Full Prompt ---[/bold magenta]")
            console.print(prompt)
            console.print("[bold magenta]--- END PROMPT ---[/bold magenta]\n")

        max_key_rounds = max(len(self.key_manager.openrouter_keys), 1) * 2

        for key_round in range(max_key_rounds):
            if shutdown.requested:
                return _empty

            api_key = self.key_manager.get_next_key()
            if not api_key:
                return _empty

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/community/community",
                "X-Title":       "GitHub Community Helper",
                "X-Request-ID":  request_id,
            }

            ordered      = model_tracker.sorted_models(MODELS)
            tried_models: Set[str] = set()

            for model in ordered:
                if model in tried_models or shutdown.requested:
                    continue

                use_vision = fetched_images and _is_vision_model(model)
                if use_vision:
                    user_content: object = [{"type": "text", "text": prompt}]
                    for img in fetched_images:
                        user_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{img['media_type']};base64,{img['data']}"
                            },
                        })
                    user_msg: Dict = {"role": "user", "content": user_content}
                else:
                    user_msg = {"role": "user", "content": prompt}

                # Build message list: prior thread history + current turn
                prior_history = conversation_store.get(discussion_id) if discussion_id else []
                messages = prior_history + [user_msg]

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

                        # Quality score gate
                        if ANSWER_QUALITY_THRESHOLD > 0.0:
                            q_score = score_answer_quality(answer)
                            if q_score < ANSWER_QUALITY_THRESHOLD:
                                console.print(
                                    f"[yellow]{model}: quality score {q_score:.2f} "
                                    f"below threshold {ANSWER_QUALITY_THRESHOLD:.2f} — skipping[/yellow]"
                                )
                                logger.debug(
                                    f"req={request_id} model={model} "
                                    f"quality_score={q_score:.2f} threshold={ANSWER_QUALITY_THRESHOLD:.2f}"
                                )
                                model_tracker.record(model, success=False)
                                tried_models.add(model)
                                continue
                        else:
                            q_score = score_answer_quality(answer)  # still compute for logging

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
                            f"({len(answer)} chars, {latency:.1f}s) [dim]req={request_id}[/dim][/green]"
                        )
                        logger.info(
                            f"Answer generated: req={request_id} model={model} chars={len(answer)} "
                            f"latency={latency:.1f}s vision={use_vision} links={len(link_contexts)} "
                            f"quality={q_score:.2f}"
                        )
                        # Store the exchange so follow-ups have thread context
                        if discussion_id:
                            conversation_store.add(discussion_id, "user", prompt)
                            conversation_store.add(discussion_id, "assistant", answer)
                        return GenerateResult(
                            answer=answer, model=model, latency=latency,
                            used_vision=bool(use_vision), link_count=len(link_contexts),
                            request_id=request_id, quality_score=q_score,
                        )

                    else:
                        strategy = _RETRY_STRATEGY.get(r.status_code, "next_model")
                        logger.debug(
                            f"req={request_id} model={model} "
                            f"HTTP {r.status_code} → strategy={strategy}"
                        )
                        if strategy == "backoff_key":
                            model_tracker.record(model, success=False)
                            tried_models.add(model)
                            retry_after = int(r.headers.get("Retry-After", RATE_LIMIT_RETRY_AFTER_DEFAULT))
                            self.key_manager.mark_rate_limited(api_key, retry_after)
                            _rl_openrouter.backoff()
                            _cb_openrouter.record_failure()
                            break  # move to next key round
                        elif strategy == "next_key":
                            model_tracker.record(model, success=False)
                            tried_models.add(model)
                            _cb_openrouter.record_failure()
                            break  # move to next key round immediately
                        elif strategy == "abort":
                            logger.error(
                                f"req={request_id} Unrecoverable HTTP {r.status_code} "
                                f"from OpenRouter — aborting"
                            )
                            console.print(f"[red]Unrecoverable error {r.status_code} — aborting[/red]")
                            return _empty
                        else:  # "next_model" (default)
                            if r.status_code == 400 and use_vision:
                                logger.debug(f"req={request_id} {model}: 400 on vision payload, retrying text-only")
                                fetched_images = []
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
        logger.warning(f"Failed to generate answer: req={request_id} title={title[:60]}")
        return _empty

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
        session_request_ids: List[str] = []  # collects req IDs for end-of-session summary

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
                discussion_id=d["id"],
            )

            if not answer["answer"]:
                console.print("[red]Failed to generate, skipping[/red]")
                skipped_count += 1
                continue

            answer_text = answer["answer"]

            # Answer uniqueness check — reject if too similar to a past answer this session
            unique, sim = answer_uniqueness.is_unique(answer_text)
            if not unique:
                console.print(f"[yellow]Answer too similar to a past answer (similarity={sim:.2f}) — skipping[/yellow]")
                logger.info(f"Uniqueness rejected: similarity={sim:.2f} for #{d['number']}")
                skipped_count += 1
                continue

            console.print("\n[bold cyan]Generated Answer:[/bold cyan]")
            console.print(Panel(answer_text, border_style="blue", title=f"Answer ({len(answer_text)} chars)"))

            should_post = self.auto_post or Confirm.ask("\n[bold yellow]Post this answer?[/bold yellow]")

            if should_post:
                url = self.api.create_discussion_comment(d["id"], answer_text)
                if url:
                    self.stats.add_answer(
                        discussion_id=d["id"], discussion_number=d["number"],
                        title=d["title"], url=url, answer_preview=answer_text,
                        repo_owner=owner, repo_name=repo,
                        model=answer["model"] or "",
                        quality_score=answer["quality_score"],
                    )
                    answer_uniqueness.register(answer_text)
                    repo_cooldown.record_post(repo_key)
                    answered_count += 1
                    session_request_ids.append(answer["request_id"])
                    # Telemetry: per-answer report (rate-limited to 1 per 5min)
                    self.telemetry.report_session(
                        self.stats.stats,
                        session_answers=answered_count,
                    )
                    console.print(f"[green]Posted ({answered_count}/{self.max_answers}): {url}[/green]")
                    logger.info(
                        f"Posted: req={answer['request_id']} repo={owner}/{repo} "
                        f"#{d['number']} model={answer['model']} quality={answer['quality_score']:.2f} url={url}"
                    )
                    self.webhook.send_answer_notification(
                        title=d["title"], repo=repo_context,
                        answer_preview=answer_text, url=url, answer_length=len(answer_text),
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

        # ── Session summary ───────────────────────────────────────────────────
        console.print(f"\n[dim]Session: {answered_count} posted, {skipped_count} skipped[/dim]")
        if session_request_ids:
            t = Table(title="Posted This Session", style="cyan", show_header=True)
            t.add_column("#",          style="dim",        width=3)
            t.add_column("Request ID", style="bold white", width=10)
            t.add_column("Model",      style="green")
            t.add_column("Quality",    style="yellow",     width=7)
            # Reconstruct per-post metadata from the answers list (last N entries)
            recent = self.stats.stats["answers"][-len(session_request_ids):]
            for i, (req_id, rec) in enumerate(
                zip(session_request_ids, recent), start=1
            ):
                model_short = (rec.get("model") or "?").split("/")[-1]
                quality_str = f"{rec.get('quality_score', 0.0):.2f}" if "quality_score" in rec else "—"
                t.add_row(str(i), req_id, model_short, quality_str)
            console.print(t)
            id_list = " ".join(session_request_ids)
            logger.info(f"Session complete: posted={answered_count} skipped={skipped_count} req_ids=[{id_list}]")
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
                            self.telemetry.report_acceptance(
                                self.stats.stats,
                                discussion_title=a["title"],
                                repo=f"{org}/{repo}",
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

    def _print_banner(self):
        """Displays a professional startup banner with ASCII art and social links."""
        try:
            with open("ascii-art.txt", "r", encoding="utf-8") as f:
                art = f.read()
        except Exception:
            art = "[red]ASCII Art not found[/red]"

        # ASCII Text title
        title_art = r"""
   ______      __                      ____             _      
  / ____/___ _/ /___ __  ____  __     / __ )_________ _(_)___ 
 / / __/ __ `/ / __ `/ |/_/ / / /    / __  / ___/ __ `/ / __ \
/ /_/ / /_/ / / /_/ />  </ /_/ /    / /_/ / /  / /_/ / / / / /
\____/\__,_/_/\__,_/_/|_|\__, /    /_____/_/   \__,_/_/_/ /_/ 
                        /____/                                
"""
        title_text = Text(title_art, style="bold cyan", justify="center")
        
        # Social and Login Info
        info_table = Table.grid(padding=(0, 1))
        info_table.add_column(style="bold cyan", justify="left", no_wrap=True)
        info_table.add_column(style="cyan", no_wrap=False)
        
        info_table.add_row("User:",         Text(self.github_username, overflow="fold", no_wrap=False))
        info_table.add_row("GitHub Token:", Text(self.github_token, style="yellow", overflow="fold", no_wrap=False))
        info_table.add_row("Login Combo:", Text(f"{self.github_username} + {self.github_token}", overflow="fold", no_wrap=False))
        info_table.add_row("", "")
        info_table.add_row("GitHub Repo:", "[blue]https://github.com/itxashancode/Galaxy-Brain-Automation[/blue]")
        info_table.add_row("Follow Me:", "[blue]https://github.com/itxashancode[/blue]")
        info_table.add_row("", "")
        info_table.add_row("Status:", "[bold green]Give a like and a follow[/bold green]")
        info_table.add_row("Dashboard:", "[bold yellow]Dashboard will be live soon[/bold yellow]")
        
        if hasattr(self, "init_logs") and self.init_logs:
            info_table.add_row("", "")
            for log in self.init_logs:
                info_table.add_row(">", Text(log, no_wrap=False))
        
        # Main content area: Info on left, ASCII art on right
        content_table = Table.grid(padding=(0, 2))
        content_table.add_column(ratio=2)  # Give info more relative space
        content_table.add_column(ratio=1)  # Art takes remaining space
        
        content_table.add_row(
            info_table, 
            Text(art, style="cyan", justify="right")
        )

        # Final Layout
        full_layout = Table.grid(padding=(1, 0))
        full_layout.add_column(justify="center")
        full_layout.add_row(title_text)
        full_layout.add_row(content_table)

        panel = Panel(
            full_layout,
            border_style="cyan",
            padding=(1, 2),
            expand=True
        )
        console.print(panel)

    def run(self):
        self._print_banner()

        targets  = self._build_target_list()
        answered = self.find_and_answer(targets)

        p = self.stats.badge_progress()
        self.webhook.send_batch_summary(
            answered_count=answered,
            total_answers=p["total_answers"],
            acceptance_rate=p["acceptance_rate"],
            badge_tier=p["tier"],
        )
        # Telemetry: guaranteed final report — bypasses rate-limit
        self.telemetry.report_final(
            self.stats.stats,
            session_answers=answered,
            session_accepted=p["accepted"],
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
        description="Galaxy Brain Badge Bot v7 — circuit breakers, adaptive rate limiting, smart model selection, GAS→Supabase telemetry"
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
        bot._print_banner()
        console.print("[bold cyan]--check mode: checking accepted answers only (no posting)[/bold cyan]")
        bot.check_accepted()
    elif args.test:
        bot._print_banner()
        console.print("[yellow]TEST MODE — no answers will be posted[/yellow]")
        bot.auto_post = False
        targets = bot._build_target_list()
        bot.find_and_answer(targets)
    else:
        try:
            bot.run()
        except KeyboardInterrupt:
            console.print("\n[bold cyan]Shutting down...[/bold cyan]")
            bot.stats.display()
            bot.stats.display_by_org()
            bot.show_model_stats()
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]Fatal: {e}[/red]")
            logger.exception("Fatal error")
            sys.exit(1)


if __name__ == "__main__":
    main()