"""AlertMate Flask application: APIs, admin UI, scheduler, hot-reload hook."""

from __future__ import annotations

import logging
import sys
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any, Deque, Dict

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request
from redis.exceptions import RedisError

from alert_processor import Alert, AlertProcessor
from config import settings, start_hot_reload
from notifier import Notifier

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("alertmate")

app = Flask(__name__)
processor = AlertProcessor()
notifier = Notifier()
scheduler = BackgroundScheduler()
_recent: Deque[Dict[str, Any]] = deque(maxlen=50)
_recent_lock = Lock()
_redis_url_snapshot = settings.redis_url


def _remember(entry: Dict[str, Any]) -> None:
    with _recent_lock:
        _recent.appendleft(entry)


def _recent_list() -> list:
    with _recent_lock:
        return list(_recent)


def _parse_until(raw: str) -> datetime:
    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("until must be ISO-8601 datetime") from exc


def _yesterday() -> str:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(settings.timezone))
    return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")


def _today() -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


def send_daily_report() -> None:
    day = _yesterday()
    try:
        top = processor.top_alerts(day, limit=5)
    except RedisError:
        logger.exception("failed to load yesterday stats for report")
        return
    try:
        notifier.send_daily_report(day, top)
    except Exception:
        logger.exception("failed to send daily report")


def _configure_scheduler() -> None:
    job_id = "daily_report"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_daily_report,
        "cron",
        hour=settings.report_hour,
        minute=settings.report_minute,
        timezone=settings.timezone,
        id=job_id,
        replace_existing=True,
    )
    logger.info(
        "daily report scheduled at %02d:%02d %s",
        settings.report_hour,
        settings.report_minute,
        settings.timezone,
    )


def _on_config_reload(_) -> None:
    global _redis_url_snapshot
    logging.getLogger().setLevel(settings.log_level)
    if settings.redis_url != _redis_url_snapshot:
        logger.info("REDIS_URL changed, reconnecting")
        processor.reconnect()
        _redis_url_snapshot = settings.redis_url
    try:
        _configure_scheduler()
    except Exception:
        logger.exception("failed to reschedule jobs after config reload")


def _process_and_notify(alert: Alert) -> Dict[str, Any]:
    result = processor.process(alert)
    if result.sent and result.alert_type:
        try:
            ok = notifier.send_alert(
                source=alert.source,
                name=alert.name,
                level=alert.level,
                target=alert.target,
                detail=alert.detail,
                count=result.count,
                alert_type=result.alert_type,
            )
            if not ok:
                result.reason = "notify attempted but webhook failed or none configured"
        except Exception:
            logger.exception("notifier raised")
            result.reason = "notifier error"
    payload = result.to_dict()
    _remember(
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": alert.source,
            "name": alert.name,
            "level": alert.level,
            "target": alert.target,
            **payload,
        }
    )
    return payload


@app.get("/health")
def health():
    redis_ok = processor.ping()
    body = {
        "status": "ok" if redis_ok else "degraded",
        "redis": "ok" if redis_ok else "error",
        "service": "alertmate",
    }
    return jsonify(body), 200 if redis_ok else 503


@app.post("/alert")
def receive_alert():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON body required"}), 400
    try:
        alert = Alert.from_payload(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    try:
        payload = _process_and_notify(alert)
    except RedisError:
        logger.exception("redis unavailable")
        return jsonify({"ok": False, "error": "redis unavailable"}), 503
    except Exception:
        logger.exception("unexpected error processing alert")
        return jsonify({"ok": False, "error": "internal error"}), 500
    return jsonify(payload), 200


@app.post("/silence")
def silence():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON body required"}), 400
    name = str(data.get("name") or "").strip()
    target = str(data.get("target") or "").strip()
    duration = data.get("duration_seconds")
    until_raw = data.get("until")
    try:
        until = _parse_until(str(until_raw)) if until_raw else None
        seconds = int(duration) if duration is not None and until is None else None
        ttl, expires_at = processor.set_silence(
            name, target, duration_seconds=seconds, until=until
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RedisError:
        logger.exception("redis unavailable while setting silence")
        return jsonify({"ok": False, "error": "redis unavailable"}), 503
    return jsonify(
        {"ok": True, "name": name, "target": target, "ttl": ttl, "expires_at": expires_at}
    )


@app.post("/webhook/prometheus")
def prometheus_webhook():
    """Map Alertmanager webhook payloads onto /alert processing."""
    data = request.get_json(silent=True) or {}
    alerts = data.get("alerts") or []
    results = []
    for item in alerts:
        if str(item.get("status", "")).lower() == "resolved":
            continue
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        severity = str(labels.get("severity") or labels.get("level") or "warning")
        sev = severity.lower()
        if sev in {"critical", "p0", "fatal", "page"}:
            level = "P0"
        elif sev in {"warning", "p1", "error"}:
            level = "P1"
        else:
            level = "P2"
        payload = {
            "source": "prometheus",
            "name": labels.get("alertname") or "unknown",
            "level": level,
            "target": labels.get("instance") or labels.get("job") or "unknown",
            "detail": annotations.get("description")
            or annotations.get("summary")
            or str(annotations),
        }
        try:
            alert = Alert.from_payload(payload)
            results.append(_process_and_notify(alert))
        except RedisError:
            return jsonify({"ok": False, "error": "redis unavailable"}), 503
        except Exception:
            logger.exception("failed to process prometheus alert")
            results.append({"ok": False, "error": "internal error", "payload": payload})
    return jsonify({"ok": True, "processed": results})


@app.get("/")
@app.get("/ui")
def ui():
    redis_ok = processor.ping()
    try:
        top = processor.top_alerts(_today(), limit=5) if redis_ok else []
    except RedisError:
        top = []
        redis_ok = False
    recent = _recent_list()
    today_count = int(sum(score for _, score in top))
    top_max = max((score for _, score in top), default=1)
    return render_template(
        "index.html",
        redis_ok=redis_ok,
        config=settings.masked(),
        top=top,
        top_max=top_max,
        recent=recent,
        today=_today(),
        today_count=today_count,
        recent_sent=sum(1 for row in recent if row.get("sent")),
        wecom_count=len(settings.wecom_webhooks),
        dingtalk_on=settings.dingtalk_enabled,
    )


@app.post("/ui/silence")
def ui_silence():
    name = (request.form.get("name") or "").strip()
    target = (request.form.get("target") or "").strip()
    duration = request.form.get("duration_seconds") or "3600"
    try:
        ttl, expires_at = processor.set_silence(
            name, target, duration_seconds=int(duration)
        )
        _remember(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "ui",
                "name": name,
                "level": "-",
                "target": target,
                "ok": True,
                "sent": False,
                "count": 0,
                "silenced": True,
                "reason": f"silence {ttl}s until {expires_at}",
            }
        )
    except Exception as exc:
        logger.warning("ui silence failed: %s", exc)
    return ui()


def create_app() -> Flask:
    return app


def main() -> None:
    start_hot_reload(_on_config_reload)
    if not scheduler.running:
        scheduler.configure(timezone=settings.timezone)
        _configure_scheduler()
        scheduler.start()
    logger.info("AlertMate listening on %s:%s", settings.host, settings.port)
    app.run(host=settings.host, port=settings.port, debug=settings.debug, use_reloader=False)


if __name__ == "__main__":
    main()
