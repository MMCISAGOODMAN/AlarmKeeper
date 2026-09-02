"""Alert fingerprinting, Redis window counting, silence, and daily stats."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from redis import Redis
from redis.exceptions import RedisError

from config import settings

logger = logging.getLogger("alertmate.processor")

SEP = "\x1f"
VALID_LEVELS = {"P0", "P1", "P2"}


@dataclass
class Alert:
    source: str
    name: str
    level: str
    target: str
    detail: str

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Alert":
        missing = [
            key
            for key in ("source", "name", "level", "target", "detail")
            if payload.get(key) is None or str(payload.get(key)).strip() == ""
        ]
        if missing:
            raise ValueError(f"missing fields: {', '.join(missing)}")
        level = str(payload["level"]).strip().upper()
        return cls(
            source=str(payload["source"]).strip(),
            name=str(payload["name"]).strip(),
            level=level,
            target=str(payload["target"]).strip(),
            detail=str(payload["detail"]).strip(),
        )


@dataclass
class ProcessResult:
    ok: bool
    sent: bool
    count: int
    level: str
    alert_type: Optional[str]
    silenced: bool
    reason: str = ""
    fingerprint: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "ok": self.ok,
            "sent": self.sent,
            "count": self.count,
            "level": self.level,
            "alert_type": self.alert_type,
            "silenced": self.silenced,
        }
        if self.reason:
            data["reason"] = self.reason
        return data


def fingerprint(*parts: str) -> str:
    raw = SEP.join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


class AlertProcessor:
    def __init__(self, redis_client: Optional[Redis] = None) -> None:
        self._redis = redis_client

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(
                settings.redis_url, decode_responses=True, socket_timeout=3
            )
        return self._redis

    def reconnect(self) -> None:
        """Drop the cached client so the next call uses current REDIS_URL."""
        old = self._redis
        self._redis = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def ping(self) -> bool:
        try:
            return bool(self.redis.ping())
        except RedisError:
            logger.exception("redis ping failed")
            return False

    def _count_key(self, fp: str) -> str:
        return f"alert:count:{fp}"

    def _silence_key(self, name: str, target: str) -> str:
        return f"alert:silence:{fingerprint(name, target)}"

    def _stats_key(self, day: str) -> str:
        return f"alert:stats:{day}"

    def bump_window_count(self, fp: str) -> int:
        """INCR inside a pipeline; set TTL only when the window is created."""
        key = self._count_key(fp)
        ttl = int(settings.alert_window_ttl)
        pipe = self.redis.pipeline(transaction=True)
        pipe.incr(key)
        pipe.ttl(key)
        count, remaining = pipe.execute()
        count = int(count)
        remaining = int(remaining)
        if count == 1 or remaining < 0:
            expire_pipe = self.redis.pipeline(transaction=True)
            expire_pipe.expire(key, ttl)
            expire_pipe.execute()
        return count

    def is_silenced(self, name: str, target: str) -> bool:
        try:
            return bool(self.redis.exists(self._silence_key(name, target)))
        except RedisError:
            logger.exception("silence lookup failed")
            return False

    def set_silence(
        self,
        name: str,
        target: str,
        duration_seconds: Optional[int] = None,
        until: Optional[datetime] = None,
    ) -> Tuple[int, str]:
        if not name or not target:
            raise ValueError("name and target are required")
        tz = ZoneInfo(settings.timezone)
        now = datetime.now(tz)
        if until is not None:
            if until.tzinfo is None:
                until = until.replace(tzinfo=tz)
            seconds = int((until - now).total_seconds())
        elif duration_seconds is not None:
            seconds = int(duration_seconds)
        else:
            raise ValueError("duration_seconds or until is required")
        if seconds <= 0:
            raise ValueError("silence duration must be in the future")
        key = self._silence_key(name, target)
        expires_at = now + timedelta(seconds=seconds)
        self.redis.set(key, expires_at.isoformat(), ex=seconds)
        logger.info("silence set name=%s target=%s ttl=%ss", name, target, seconds)
        return seconds, expires_at.isoformat()

    def record_stats(self, alert: Alert) -> None:
        day = _now(settings.timezone).strftime("%Y-%m-%d")
        member = f"{alert.source}|{alert.name}|{alert.target}"
        key = self._stats_key(day)
        pipe = self.redis.pipeline(transaction=True)
        pipe.zincrby(key, 1, member)
        pipe.expire(key, int(settings.stats_ttl))
        pipe.execute()

    def top_alerts(self, day: str, limit: int = 5) -> List[Tuple[str, float]]:
        key = self._stats_key(day)
        rows = self.redis.zrevrange(key, 0, limit - 1, withscores=True)
        return [(member, float(score)) for member, score in rows]

    def decide(self, level: str, count: int) -> Tuple[bool, Optional[str], str]:
        """Return (should_send, alert_type, reason)."""
        if level not in VALID_LEVELS:
            logger.warning("unknown level %s, treating as P2", level)
            return False, None, "unknown level treated as P2 (log only)"
        if level == "P2":
            return False, None, "P2 is log-only"
        if level == "P0":
            if count == 1:
                return True, "首次告警", "P0 first occurrence"
            return False, None, "P0 already notified in window"
        # P1
        if count == 1:
            return True, "首次告警", "P1 first occurrence"
        if count == 3:
            return True, "持续告警", "P1 reached 3 occurrences"
        return False, None, "P1 waiting for threshold"

    def process(self, alert: Alert) -> ProcessResult:
        fp = fingerprint(alert.source, alert.name, alert.target)
        try:
            count = self.bump_window_count(fp)
            self.record_stats(alert)
        except RedisError:
            logger.exception("redis error while processing alert")
            raise

        silenced = self.is_silenced(alert.name, alert.target)
        if silenced:
            logger.info(
                "alert silenced fp=%s name=%s target=%s count=%s",
                fp,
                alert.name,
                alert.target,
                count,
            )
            return ProcessResult(
                ok=True,
                sent=False,
                count=count,
                level=alert.level if alert.level in VALID_LEVELS else "P2",
                alert_type=None,
                silenced=True,
                reason="silenced",
                fingerprint=fp,
            )

        should_send, alert_type, reason = self.decide(alert.level, count)
        level = alert.level if alert.level in VALID_LEVELS else "P2"
        if not should_send:
            logger.info(
                "alert not sent level=%s name=%s target=%s count=%s reason=%s",
                level,
                alert.name,
                alert.target,
                count,
                reason,
            )
        return ProcessResult(
            ok=True,
            sent=should_send,
            count=count,
            level=level,
            alert_type=alert_type,
            silenced=False,
            reason=reason,
            fingerprint=fp,
        )
