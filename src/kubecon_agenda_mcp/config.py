"""Event configuration.

Everything event-specific lives here and is overridable through environment
variables, so the same server can be pointed at any conference that publishes
a Sessionize schedule (KubeCon EU, Open Source Summit, ...).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class EventConfig:
    name: str
    short_name: str
    sessionize_id: str
    timezone: str
    official_url: str
    city: str
    cache_ttl: int

    @property
    def feed_url(self) -> str:
        return f"https://sessionize.com/api/v2/{self.sessionize_id}/view/All"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config() -> EventConfig:
    env = os.environ.get
    return EventConfig(
        name=env("EVENT_NAME", "KubeCon + CloudNativeCon North America 2026"),
        short_name=env("EVENT_SHORT_NAME", "KubeCon NA 2026"),
        sessionize_id=env("SESSIONIZE_EVENT_ID", "svi82w6c"),
        timezone=env("EVENT_TIMEZONE", "America/Denver"),
        official_url=env(
            "EVENT_URL",
            "https://events.linuxfoundation.org/kubecon-cloudnativecon-north-america/program/schedule/",
        ),
        city=env("EVENT_CITY", "Salt Lake City"),
        cache_ttl=int(env("CACHE_TTL_SECONDS", "600")),
    )
