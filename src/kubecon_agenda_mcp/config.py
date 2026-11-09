"""Event configuration.

Everything event-specific lives here and is overridable through environment
variables, so the same server can be pointed at any conference that publishes
a Sessionize schedule (KubeCon EU, Open Source Summit, ...).

A conference may spread its programme over several Sessionize events. KubeCon
NA 2026 uses three: the main conference, the CNCF-hosted co-located events and
the Maintainer Summit. `SESSIONIZE_FEEDS` lists them as `id=label` pairs
separated by commas; the label becomes the session's `program` and is used as
the track for feeds that have no track category of their own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

DEFAULT_FEEDS = "svi82w6c=Main conference,ayajbd6b=Co-located events,qtujim6y=Maintainer Summit"


@dataclass(frozen=True)
class Feed:
    sessionize_id: str
    program: str

    @property
    def url(self) -> str:
        return f"https://sessionize.com/api/v2/{self.sessionize_id}/view/All"


@dataclass(frozen=True)
class EventConfig:
    name: str
    short_name: str
    feeds: tuple[Feed, ...]
    timezone: str
    official_url: str
    city: str
    cache_ttl: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def parse_feeds(spec: str) -> tuple[Feed, ...]:
    feeds = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        sid, _, label = part.partition("=")
        feeds.append(Feed(sid.strip(), label.strip() or sid.strip()))
    if not feeds:
        raise ValueError("SESSIONIZE_FEEDS must list at least one Sessionize event id")
    return tuple(feeds)


def load_config() -> EventConfig:
    env = os.environ.get
    spec = env("SESSIONIZE_FEEDS")
    if not spec:
        single = env("SESSIONIZE_EVENT_ID")
        spec = f"{single}=Main conference" if single else DEFAULT_FEEDS
    return EventConfig(
        name=env("EVENT_NAME", "KubeCon + CloudNativeCon North America 2026"),
        short_name=env("EVENT_SHORT_NAME", "KubeCon NA 2026"),
        feeds=parse_feeds(spec),
        timezone=env("EVENT_TIMEZONE", "America/Denver"),
        official_url=env(
            "EVENT_URL",
            "https://events.linuxfoundation.org/kubecon-cloudnativecon-north-america/program/schedule/",
        ),
        city=env("EVENT_CITY", "Salt Lake City"),
        cache_ttl=int(env("CACHE_TTL_SECONDS", "600")),
    )
