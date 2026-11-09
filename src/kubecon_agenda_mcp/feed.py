"""Fetch one or more Sessionize "All" views and turn them into Session objects.

Sessionize's feed is normalised relational data: sessions reference rooms,
speakers and category items by id. We join everything once per fetch and keep
the result in memory for `cache_ttl` seconds. If a refresh fails we keep
serving the previous copy rather than returning an error to the agent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import EventConfig, Feed

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

# Tracks / categories that mark an entry as not-a-talk: breaks, registration desks and
# social events (receptions, group photos, pet-a-pup...). Checked against tracks and tags.
NON_TALK_LABELS = {
    "Breaks",
    "☕ Breaks",
    "Breaks + Networking + Special Events",
    "Registration",
    "Experiences",
    "🎉 Experiences",
}
# A few break entries carry no break category at all (e.g. coffee breaks filed under
# "Project Lightning Talks"). Only an *exact* normalised title can mark those, so a talk
# with "break" in its name can never be hidden.
NON_TALK_TITLES = {
    "break", "am break", "pm break", "coffee break", "lunch", "lunch break",
    "coffee + networking", "coffee and networking", "networking break",
    "registration + badge pick-up", "registration + badge pickup", "registration",
    "coat + bag check", "group photo",
}
OFFSITE_VENUE = "Off-Site Location"

# LF appends access notes to titles: "ArgoCon ... | ALL ACCESS PASS REQUIRED".
_ACCESS_SEG = r"(?:ALL ACCESS|SEPARATE REGISTRATION|SOLD OUT|INVITE|PRE-REGISTRATION|RSVP)[^|]*"
_ACCESS_RE = re.compile(rf"((?:\s*(?:\|\s*{_ACCESS_SEG}|\[\s*{_ACCESS_SEG}\]))+)$", re.I)
_TITLE_NORM_RE = re.compile(r"[^a-z0-9+ ]+")


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
        n = needle.strip().lower()
        if n in self.name.lower():
            return True
        return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", f"{self.company} {self.tagline}".lower()) is not None


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
    tracks: tuple[str, ...]  # every track the session is filed under; `track` is the first
    topics: tuple[str, ...]
    projects: tuple[str, ...]
    tags: tuple[str, ...]
    speakers: tuple[Speaker, ...]
    links: dict[str, str] = field(compare=False)
    is_plenum: bool = False
    program: str = ""  # which feed this came from, e.g. "Co-located events"
    access: str = ""  # "ALL ACCESS PASS REQUIRED", "SEPARATE REGISTRATION REQUIRED", "SOLD OUT", ...

    @property
    def is_talk(self) -> bool:
        """False for breaks, registration desks and social events.

        Decided from the feed's own categories; the only title-based rule is an exact match
        against a short list of generic break names, never a keyword search."""
        if any(label in NON_TALK_LABELS for label in (*self.tracks, *self.tags)):
            return False
        norm = " ".join(_TITLE_NORM_RE.sub(" ", self.title.lower()).split())
        return norm not in NON_TALK_TITLES

    @property
    def is_colocated(self) -> bool:
        return "Co-located" in self.track or "Co-located" in self.program

    @property
    def is_offsite(self) -> bool:
        return self.venue == OFFSITE_VENUE

    @property
    def day(self) -> str:
        return self.start.date().isoformat()

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_block(self) -> bool:
        """All-day umbrellas (a co-located day, the showcase floor) rather than a single talk."""
        return self.minutes > 150

    def overlaps(self, other: "Session") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class Agenda:
    sessions: list[Session]
    speakers: list[Speaker]
    fetched_at: float
    programs: tuple[str, ...] = ()
    etags: dict[str, str] = field(default_factory=dict)
    unavailable: tuple[str, ...] = ()  # programs whose feed could not be loaded this time

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


def split_access(title: str) -> tuple[str, str]:
    """'ArgoCon ... | ALL ACCESS PASS REQUIRED | SOLD OUT' -> ('ArgoCon ...', 'ALL ACCESS PASS REQUIRED; SOLD OUT')."""
    m = _ACCESS_RE.search(title)
    if not m:
        return title.strip(), ""
    labels = [seg.strip().upper() for seg in re.split(r"[|\[\]]", m.group(1)) if seg.strip()]
    return title[: m.start()].strip(), "; ".join(labels)


def normalize(raw: dict, program: str = "") -> tuple[list[Session], list[Speaker]]:
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
        tracks = tuple(groups.get(TRACK_GROUP) or [program or ""])
        track = tracks[0]
        topics = tuple(t for t in groups.get(TOPIC_GROUP, []) if t not in tracks)
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
        title, access = split_access(s.get("title") or "")
        sessions.append(
            Session(
                id=str(s["id"]),
                title=title,
                abstract=(s.get("description") or "").strip(),
                start=_parse(s["startsAt"]),
                end=_parse(s["endsAt"]),
                room=", ".join(parts),
                venue=parts[0] if parts else "",
                track=track,
                level=(groups.get(LEVEL_GROUP) or [""])[0],
                tracks=tracks,
                topics=topics,
                projects=projects,
                tags=tags,
                speakers=tuple(speakers[i] for i in s.get("speakers") or [] if i in speakers),
                links=links,
                is_plenum=bool(s.get("isPlenumSession")),
                program=program,
                access=access,
            )
        )
    return sessions, list(speakers.values())


def _speaker_key(sp: Speaker) -> str:
    return re.sub(r"[^a-z]", "", sp.name.lower())


def _inherit_access(sessions: list[Session]) -> list[Session]:
    """Umbrella entries in the main feed ("ArgoCon Hosted by CNCF ... | ALL ACCESS PASS REQUIRED")
    carry the access rule for a whole programme. Copy it to every child talk filed under that
    track in another feed, and stretch the umbrella to the last child's end time."""
    from dataclasses import replace

    umbrellas = [s for s in sessions if s.is_block and s.access]
    out = list(sessions)
    for i, s in enumerate(out):
        if s.access or not s.program:
            continue
        for u in umbrellas:
            if u.program != s.program and u.day == s.day and any(t.lower() in u.title.lower() for t in s.tracks if t):
                out[i] = replace(s, access=u.access)
                break
    for i, u in enumerate(out):
        if u in umbrellas:
            # Only single-track children count: lunches and receptions are filed under every event.
            kids = [s for s in out if s.program != u.program and s.day == u.day and len(s.tracks) == 1 and s.tracks[0] and s.tracks[0].lower() in u.title.lower()]
            if kids:
                last = max(k.end for k in kids)
                if last > u.end:
                    out[i] = replace(u, end=last)
    return out


def merge(parts: list[tuple[list[Session], list[Speaker]]]) -> tuple[list[Session], list[Speaker]]:
    sessions: dict[str, Session] = {}
    speakers: dict[str, Speaker] = {}  # keyed by normalised name: Sessionize ids differ per event
    for ss, sps in parts:
        for s in ss:
            sessions.setdefault(s.id, s)
        for sp in sps:
            key = _speaker_key(sp)
            prev = speakers.get(key)
            if prev is None:
                speakers[key] = sp
            else:  # same person in two feeds: union their sessions and ids
                speakers[key] = Speaker(
                    prev.id, prev.name, prev.tagline or sp.tagline, prev.company or sp.company,
                    prev.bio or sp.bio, {**sp.links, **prev.links},
                    tuple(dict.fromkeys(prev.session_ids + sp.session_ids)),
                )
    # Point every session's speakers at the merged records.
    by_key = {_speaker_key(sp): sp for sp in speakers.values()}
    from dataclasses import replace
    merged = [replace(s, speakers=tuple(by_key[_speaker_key(x)] for x in s.speakers)) for s in sessions.values()]
    out = sorted(_inherit_access(merged), key=lambda x: (x.start, x.room))
    return out, sorted(speakers.values(), key=lambda sp: sp.name.lower())


class AgendaStore:
    """TTL cache around the feeds with stale-on-error behaviour."""

    def __init__(self, config: EventConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self._agenda: Agenda | None = None
        self._raw: dict[str, dict] = {}  # last good payload per feed id
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._agenda is not None and time.monotonic() - self._agenda.fetched_at < self.config.cache_ttl

    async def get(self) -> Agenda:
        if self._fresh():
            return self._agenda  # type: ignore[return-value]
        async with self._lock:
            if not self._fresh():
                await self._refresh()
        assert self._agenda is not None
        return self._agenda

    async def _fetch(self, client: httpx.AsyncClient, feed: Feed) -> tuple[Feed, httpx.Response | None]:
        headers = {"User-Agent": "kubecon-agenda-mcp (+https://github.com/slashben/kubecon-agenda-mcp)"}
        etag = self._agenda.etags.get(feed.sessionize_id) if self._agenda else None
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = await client.get(feed.url, headers=headers)
            if resp.status_code != 304:
                resp.raise_for_status()
            return feed, resp
        except Exception:
            log.warning("feed %s (%s) failed", feed.program, feed.sessionize_id, exc_info=True)
            return feed, None

    async def _refresh(self) -> None:
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            results = await asyncio.gather(*(self._fetch(client, f) for f in self.config.feeds))
        finally:
            if self._client is None:
                await client.aclose()

        etags = dict(self._agenda.etags) if self._agenda else {}
        for feed, resp in results:
            if resp is None or resp.status_code == 304:
                continue  # keep whatever we had for this feed
            self._raw[feed.sessionize_id] = resp.json()
            if resp.headers.get("etag"):
                etags[feed.sessionize_id] = resp.headers["etag"]

        if not self._raw:
            raise RuntimeError("no schedule feed could be loaded")
        parts = [normalize(self._raw[f.sessionize_id], f.program) for f in self.config.feeds if f.sessionize_id in self._raw]
        sessions, speakers = merge(parts)
        loaded = tuple(f.program for f in self.config.feeds if f.sessionize_id in self._raw)
        missing = tuple(f.program for f in self.config.feeds if f.sessionize_id not in self._raw)
        self._agenda = Agenda(sessions, speakers, time.monotonic(), loaded, etags, missing)
        if missing:
            # Retry soon rather than serving a partial agenda for a whole TTL.
            self._agenda.fetched_at = time.monotonic() - self.config.cache_ttl + 60
        log.info("feeds loaded: %s; %d sessions, %d speakers", ", ".join(loaded), len(sessions), len(speakers))

    def load_static(self, *raws: dict | tuple[dict, str]) -> Agenda:
        """Used by tests and offline runs. Each item is a payload or (payload, program)."""
        parts = []
        programs = []
        for item in raws:
            raw, program = item if isinstance(item, tuple) else (item, "")
            parts.append(normalize(raw, program))
            programs.append(program)
        sessions, speakers = merge(parts)
        self._agenda = Agenda(sessions, speakers, time.monotonic(), tuple(programs))
        return self._agenda
