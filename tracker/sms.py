"""Twilio SMS sink for top-tier alerts (back in stock, Early Access by default)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracker.alerts import AlertRouter
from tracker.domain import format_cents
from tracker.outbox import RadarEvent
from tracker.settings import SmsSettings

logger = logging.getLogger(__name__)

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def sms_body(event: RadarEvent) -> str:
    headline = event.kind.value.replace("_", " ").upper()
    parts = [f"{headline}: {event.sku or event.product_name or 'item'}"]
    if event.price_cents is not None:
        parts.append(format_cents(event.price_cents, event.currency))
    if event.region:
        parts.append(event.region.upper())
    if event.url:
        parts.append(event.url)
    return " | ".join(parts)[:320]


class SmsSink:
    name = "sms"

    def __init__(
        self,
        settings: SmsSettings,
        sessions: async_sessionmaker[AsyncSession],
        router: AlertRouter,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._router = router
        self._transport = transport
        self._kinds = set(settings.kinds)

    async def publish(self, events: Sequence[RadarEvent]) -> None:
        candidates = [e for e in events if e.kind.value in self._kinds]
        if not candidates:
            return
        async with self._sessions() as session:
            plans = await self._router.plan(session, candidates)
        messages = [sms_body(p.event) for p in plans if not p.suppressed]
        if not messages:
            return

        token = self._settings.auth_token.get_secret_value() if self._settings.auth_token else ""
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
            auth=(self._settings.account_sid, token),
            transport=self._transport,
        ) as client:
            for body in messages:
                for number in self._settings.to_numbers:
                    response = await client.post(
                        TWILIO_URL.format(sid=self._settings.account_sid),
                        data={"From": self._settings.from_number, "To": number, "Body": body},
                    )
                    if response.status_code >= 400:
                        # Don't block the outbox on a bad number; log and move on.
                        logger.error(
                            "Twilio SMS to %s failed: HTTP %s %s",
                            number,
                            response.status_code,
                            response.text[:200],
                        )
