"""Outbound WeCom / DingTalk notifications via asyncio + aiohttp."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple
from zoneinfo import ZoneInfo

import aiohttp

from config import settings

logger = logging.getLogger("alertmate.notifier")


def _now_text() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_alert_markdown(
    *,
    name: str,
    level: str,
    target: str,
    alert_type: str,
    count: int,
    detail: str,
    source: str,
    mention_all: bool = False,
) -> str:
    mention = "<@all>\n" if mention_all else ""
    return (
        f"{mention}**{alert_type} · {level}**\n"
        f"> 告警名称：{name}\n"
        f"> 告警级别：{level}\n"
        f"> 目标：{target}\n"
        f"> 告警源：{source}\n"
        f"> 告警类型：{alert_type}\n"
        f"> 累计次数：{count}\n"
        f"> 详情：{detail}\n"
        f"> 时间：{_now_text()}"
    )


def build_report_markdown(day: str, top: Sequence[Tuple[str, float]]) -> str:
    if not top:
        body = "昨日无告警记录。"
    else:
        lines = []
        for idx, (member, score) in enumerate(top, start=1):
            lines.append(f"{idx}. `{member}` — **{int(score)}** 次")
        body = "\n".join(lines)
    return (
        f"**昨日告警统计报告**\n"
        f"> 统计日期：{day}\n"
        f"> TOP 5：\n{body}\n"
        f"> 生成时间：{_now_text()}"
    )


def _wecom_payload(content: str) -> Dict[str, Any]:
    return {"msgtype": "markdown", "markdown": {"content": content}}


def _dingtalk_payload(content: str, mention_all: bool) -> Dict[str, Any]:
    title = content.split("\n", 1)[0].replace("**", "").replace("<@all>", "").strip()
    text = content.replace("<@all>", "@所有人")
    payload: Dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {"title": title or "AlertMate", "text": text},
    }
    if mention_all:
        payload["at"] = {"isAtAll": True}
    return payload


async def _post_json(
    session: aiohttp.ClientSession, url: str, payload: Dict[str, Any]
) -> bool:
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.post(url, json=payload, timeout=timeout) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "webhook failed status=%s url=%s body=%s",
                    resp.status,
                    url[:48],
                    body[:300],
                )
                return False
            logger.info("webhook ok status=%s url=%s", resp.status, url[:48])
            return True
    except Exception:
        logger.exception("webhook request error url=%s", url[:48])
        return False


async def _dispatch(
    tasks: List[Tuple[str, Dict[str, Any]]],
) -> List[bool]:
    if not tasks:
        logger.warning("no webhook urls configured; skip send")
        return []
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_post_json(session, url, payload) for url, payload in tasks],
            return_exceptions=True,
        )
    ok_flags: List[bool] = []
    for item in results:
        if isinstance(item, Exception):
            logger.exception("webhook task crashed: %s", item)
            ok_flags.append(False)
        else:
            ok_flags.append(bool(item))
    return ok_flags


def _run(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return asyncio.ensure_future(coro)
    return asyncio.run(coro)


class Notifier:
    def send_markdown(self, content: str, mention_all: bool = False) -> bool:
        tasks: List[Tuple[str, Dict[str, Any]]] = []
        wecom_body = _wecom_payload(content)
        for url in settings.wecom_webhooks:
            tasks.append((url, wecom_body))
        if settings.dingtalk_enabled:
            ding_body = _dingtalk_payload(content, mention_all)
            for url in settings.dingtalk_webhooks:
                tasks.append((url, ding_body))
        elif settings.dingtalk_webhooks:
            logger.debug("dingtalk webhooks present but DINGTALK_ENABLED is false")

        results = _run(_dispatch(tasks))
        if not isinstance(results, list):
            return True
        if not tasks:
            return False
        return any(results)

    def send_alert(
        self,
        *,
        source: str,
        name: str,
        level: str,
        target: str,
        detail: str,
        count: int,
        alert_type: str,
    ) -> bool:
        mention_all = level == "P0"
        content = build_alert_markdown(
            name=name,
            level=level,
            target=target,
            alert_type=alert_type,
            count=count,
            detail=detail,
            source=source,
            mention_all=mention_all,
        )
        logger.info(
            "sending alert type=%s level=%s name=%s target=%s count=%s",
            alert_type,
            level,
            name,
            target,
            count,
        )
        return bool(self.send_markdown(content, mention_all=mention_all))

    def send_daily_report(self, day: str, top: Sequence[Tuple[str, float]]) -> bool:
        content = build_report_markdown(day, top)
        logger.info("sending daily report for %s items=%s", day, len(top))
        return bool(self.send_markdown(content, mention_all=False))
