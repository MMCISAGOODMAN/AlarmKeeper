"""AlertMate runtime configuration.

Values are loaded from environment variables (optional `.env`).
`config.py` and `.env` are polled for mtime changes so most settings can
be hot-reloaded without restarting the process. HOST/PORT are not applied
until the next process start.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = Path(__file__).resolve()

logger = logging.getLogger("alertmate.config")

_reload_lock = threading.Lock()
_watch_thread: Optional[threading.Thread] = None
_watch_started = False


def _as_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_list(name: str) -> List[str]:
    raw = os.getenv(name, "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _mask_webhook(url: str) -> str:
    if len(url) <= 20:
        return "***"
    return f"{url[:28]}..."


class Settings:
    """Mutable settings snapshot refreshed by `reload()`."""

    def __init__(self) -> None:
        self.redis_url = "redis://127.0.0.1:6379/0"
        self.wecom_webhooks: List[str] = []
        self.dingtalk_enabled = False
        self.dingtalk_webhooks: List[str] = []
        self.alert_window_ttl = 300
        self.timezone = "Asia/Shanghai"
        self.host = "0.0.0.0"
        self.port = 8080
        self.hot_reload_interval = 2.0
        self.log_level = "INFO"
        self.report_hour = 9
        self.report_minute = 0
        self.stats_ttl = 48 * 3600
        self.debug = False

    def reload(self) -> None:
        load_dotenv(ENV_PATH, override=True)
        self.redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip()
        self.wecom_webhooks = _as_list("WECOM_WEBHOOKS")
        self.dingtalk_enabled = _as_bool("DINGTALK_ENABLED", False)
        self.dingtalk_webhooks = _as_list("DINGTALK_WEBHOOKS")
        self.alert_window_ttl = int(os.getenv("ALERT_WINDOW_TTL", "300"))
        self.timezone = os.getenv("TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"
        self.host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.port = int(os.getenv("PORT", "8080"))
        self.hot_reload_interval = float(os.getenv("HOT_RELOAD_INTERVAL", "2"))
        self.log_level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()
        self.report_hour = int(os.getenv("REPORT_HOUR", "9"))
        self.report_minute = int(os.getenv("REPORT_MINUTE", "0"))
        self.stats_ttl = int(os.getenv("STATS_TTL", str(48 * 3600)))
        self.debug = _as_bool("FLASK_DEBUG", False)

    def masked(self) -> Dict[str, object]:
        return {
            "redis_url": self.redis_url,
            "wecom_webhooks": [_mask_webhook(u) for u in self.wecom_webhooks],
            "dingtalk_enabled": self.dingtalk_enabled,
            "dingtalk_webhooks": [_mask_webhook(u) for u in self.dingtalk_webhooks],
            "alert_window_ttl": self.alert_window_ttl,
            "timezone": self.timezone,
            "host": self.host,
            "port": self.port,
            "report_hour": self.report_hour,
            "report_minute": self.report_minute,
            "stats_ttl": self.stats_ttl,
            "log_level": self.log_level,
            "note": "HOST/PORT changes require a process restart",
        }


settings = Settings()
load_dotenv(ENV_PATH)
settings.reload()


def start_hot_reload(on_reload: Optional[Callable[[Settings], None]] = None) -> None:
    """Poll `.env` and this file; refresh settings when mtime changes."""

    global _watch_started, _watch_thread
    if _watch_started:
        return
    _watch_started = True

    def _mtimes() -> Dict[str, float]:
        result: Dict[str, float] = {}
        for path in (ENV_PATH, CONFIG_PATH):
            try:
                result[str(path)] = path.stat().st_mtime
            except OSError:
                result[str(path)] = 0.0
        return result

    def _loop() -> None:
        last = _mtimes()
        while True:
            interval = max(0.5, float(settings.hot_reload_interval))
            time.sleep(interval)
            current = _mtimes()
            if current == last:
                continue
            last = current
            with _reload_lock:
                try:
                    settings.reload()
                    logging.getLogger().setLevel(settings.log_level)
                    logger.info("configuration reloaded from disk")
                    if on_reload:
                        on_reload(settings)
                except Exception:
                    logger.exception("failed to hot-reload configuration")

    _watch_thread = threading.Thread(
        target=_loop, name="alertmate-hot-reload", daemon=True
    )
    _watch_thread.start()
