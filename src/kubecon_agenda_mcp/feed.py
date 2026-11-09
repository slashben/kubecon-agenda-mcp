"""Fetch the Sessionize "All" view and turn it into plain Session objects.

Sessionize's feed is normalised relational data: sessions reference rooms,
speakers and category items by id. We join everything once per fetch and keep
the result in memory for `cache_ttl` seconds. If a refresh fails we keep
serving the previous copy rather than returning an error to the agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import EventConfig

log = logging.getLogger(__name__)

# Sessionize category groups used by LF/CNCF events.
TRACK_GROUP = "All Sessions"
LEVEL_GROUP = "Audience Level"
TOPIC_GROUP = "SubTopic"
PROJECT_GROUP = "CNCF Projects"

# Free-form "question" fields LF attaches to sessions and speakers.
SESSION_LINK_QUESTIONS = {
    "Presentation Slides": "slides",
    "GitHub Repository": "repo",
    "Project Demo": "demo",
    "Website": "website",
    "Location Map": "map",
}
SPEAKER_COMPANY_QUESTIONS = ("Company Override", "Company")

NON_TALK_TRACKS = {"Breaks", "Registration"}


@dataclass(frozen=True)
class Speaker:
    id: str
    name: str
    tagline: str
    company: str
    bio: str
    links: dict[str, str] = field(default_factory=dict, compare=False)
    session_ids: tuple[str, ...] = ()

    def matches(self, needle: str) -> bool:
        n = needle.lower()
        return n in self.name.lower() or n in self.company.lower() or n in self.tagline.lower()


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    abstract: str
    start: datetime  # naive, event-local wall clock
    end: datetime
    room: str
    venue: str  # first segment of the room name, e.g. "Salt Palace"
    track: str
    level: str
    topics: tuple[str, ...]
    projects: tuple[str, ...]
    tags: tuple[str, ...]
    speakers: tuple[Speaker, ...]
    links: dict[str, str] = field(compare=False)
    is_plenum: bool = False

    @property
    def is_talk(self) -> bool:
        """False for registration desks, breaks, lunches and similar filler."""
        if self.track in NON_TALK_TRACKS:
            return False
        head = self.title.lower()
        return not head.startswith(("break", "lunch", "coffee", "registration"))

    @property
    def is_colocated(self) -> bool:
        return "Co-located" in self.track

    @property
    def day(self) -> str:
        return self.start.date().isoformat()

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: "Session") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class Agenda:
    sessions: list[Session]
    speakers: list[Speaker]
    fetched_at: float
    etag: str | None = None

    def by_id(self, session_id: str) -> Session | None:
        return next((s for s in self.sessions if s.id == session_id), None)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])


def _answers(obj: dict, questions: dict[int, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for qa in obj.get("questionAnswers") or []:
        label = questions.get(qa.get("questionId"))
        value = (qa.get("answerValue") or "").strip()
        if label and value:
            out[label] = value
    return out


def normalize(raw: dict) -> tuple[list[Session], list[Speaker]]:
    questions = {q["id"]: q["question"] for q in raw.get("questions", [])}
    rooms = {r["id"]: r["name"].strip() for r in raw.get("rooms", [])}

    categories: dict[int, tuple[str, str]] = {}
    for group in raw.get("categories", []):
        for item in group.get("items", []):
            categories[item["id"]] = (group["title"], item["name"].strip())

    speakers: dict[str, Speaker] = {}
    for sp in raw.get("speakers", []):
        ans = _answers(sp, questions)
        company = next((ans[q] for q in SPEAKER_COMPANY_QUESTIONS if q in ans), "")
        tagline = (sp.get("tagLine") or "").strip()
        if not company and "," in tagline:
            company = tagline.split(",", 1)[0].strip()
        speakers[sp["id"]] = Speaker(
            id=sp["id"],
            name=(sp.get("fullName") or f'{sp.get("firstName", "")} {sp.get("lastName", "")}').strip(),
            tagline=tagline,
            company=company,
            bio=(sp.get("bio") or "").strip(),
            links={lk["title"]: lk["url"] for lk in sp.get("links") or [] if lk.get("url")},
            session_ids=tuple(str(x) for x in sp.get("sessions") or []),
        )

    sessions: list[Session] = []
    for s in raw.get("sessions", []):
        if not (s.get("startsAt") and s.get("endsAt")):
            continue
        groups: dict[str, list[str]] = {}
        for cid in s.get("categoryItems") or []:
            if cid in categories:
                g, name = categories[cid]
                groups.setdefault(g, []).append(name)
        track = (groups.get(TRACK_GROUP) or [""])[0]
        topics = tuple(t for t in groups.get(TOPIC_GROUP, []) if t != track)
        projects = tuple(groups.get(PROJECT_GROUP, []))
        tags = tuple(
            name
            for g, names in groups.items()
            if g not in (TRACK_GROUP, LEVEL_GROUP, TOPIC_GROUP, PROJECT_GROUP)
            for name in names
        )
        ans = _answers(s, questions)
        links = {short: ans[q] for q, short in SESSION_LINK_QUESTIONS.items() if q in ans}
        # Sessionize rooms look like "Salt Palace | Level 1 | Grand Ballroom H".
        room_raw = rooms.get(s.get("roomId"), "")
        parts = [p.strip() for p in room_raw.split("|") if p.strip()]
        sessions.append(
            Session(
                id=str(s["id"]),
                title=(s.get("title") or "").strip(),
                abstract=(s.get("description") or "").strip(),
                start=_parse(s["startsAt"]),
                end=_parse(s["endsAt"]),
                room=", ".join(parts),
                venue=parts[0] if parts else "",
                track=track,
                level=(groups.get(LEVEL_GROUP) or [""])[0],
                topics=topics,
                projects=projects,
                tags=tags,
                speakers=tuple(speakers[i] for i in s.get("speakers") or [] if i in speakers),
                links=links,
                is_plenum=bool(s.get("isPlenumSession")),
            )
        )
    sessions.sort(key=lambda x: (x.start, x.room))
    return sessions, sorted(speakers.values(), key=lambda sp: sp.name.lower())


class AgendaStore:
    """TTL cache around the feed with stale-on-error behaviour."""

    def __init__(self, config: EventConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self._agenda: Agenda | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._agenda is not None and time.monotonic() - self._agenda.fetched_at < self.config.cache_ttl

    async def get(self) -> Agenda:
        if self._fresh():
            return self._agenda  # type: ignore[return-value]
        async with self._lock:
            if not self._fresh():
                try:
                    await self._refresh()
                except Exception:  # network, parse, 5xx ...
                    if self._agenda is None:
                        raise
                    log.warning("feed refresh failed; serving cached copy", exc_info=True)
                    self._agenda.fetched_at = time.monotonic()
        assert self._agenda is not None
        return self._agenda

    async def _refresh(self) -> None:
        headers = {"User-Agent": "kubecon-agenda-mcp (+https://github.com/slashben/kubecon-agenda-mcp)"}
        if self._agenda and self._agenda.etag:
            headers["If-None-Match"] = self._agenda.etag
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            resp = await client.get(self.config.feed_url, headers=headers)
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code == 304 and self._agenda:
            self._agenda.fetched_at = time.monotonic()
            return
        resp.raise_for_status()
        sessions, speakers = normalize(resp.json())
        self._agenda = Agenda(sessions, speakers, time.monotonic(), resp.headers.get("etag"))
        log.info("feed loaded: %d sessions, %d speakers", len(sessions), len(speakers))

    def load_static(self, raw: dict) -> Agenda:
        """Used by tests and offline runs."""
        sessions, speakers = normalize(raw)
        self._agenda = Agenda(sessions, speakers, time.monotonic())
        return self._agenda
