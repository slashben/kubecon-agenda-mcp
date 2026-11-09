"""Querying and formatting the agenda.

Design notes
- Search is AND over terms (predictable for agents) but results are *ranked*:
  a term in the title beats one in a tag, which beats one in the abstract.
- Output is terse, one line per session, because agents pay for every token.
- All times are the event's local wall clock; nothing here is timezone-aware
  except `now_local`, which converts the real clock into event time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import EventConfig
from .feed import Agenda, Session, Speaker

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?\s*$", re.I)


@dataclass
class Query:
    text: str | None = None
    speaker: str | None = None
    day: str | None = None
    after: time | None = None
    before: time | None = None
    room: str | None = None
    track: str | None = None
    project: str | None = None
    level: str | None = None
    include_breaks: bool = False
    include_offsite: bool = True


def parse_time(value: str | None) -> time | None:
    """Accept 14:00, 9:30, 2pm, 9:30am, 14. Raise ValueError otherwise."""
    if value is None or not value.strip():
        return None
    m = _TIME_RE.match(value)
    if not m:
        raise ValueError(f"cannot read time {value!r}; use 14:00, 2pm or 9:30am")
    hour, minute, suffix = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    if suffix:
        if not 1 <= hour <= 12:
            raise ValueError(f"cannot read time {value!r}")
        hour = hour % 12 + (12 if suffix.startswith("p") else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"cannot read time {value!r}")
    return time(hour, minute)


def event_days(agenda: Agenda) -> list[str]:
    return sorted({s.day for s in agenda.sessions})


def resolve_day(value: str | None, agenda: Agenda) -> str | None:
    """Turn 'tue', 'Tuesday', '11-10' or '2026-11-10' into an ISO date present in the agenda."""
    if not value:
        return None
    v = value.strip().lower()
    days = event_days(agenda)
    if v in days:
        return v
    for d in days:
        if d[5:] == v or d[8:] == v.zfill(2):
            return d
    if v[:3] in WEEKDAYS:
        for d in days:
            if WEEKDAYS[datetime.fromisoformat(d).weekday()] == v[:3]:
                return d
    raise ValueError(f"unknown day {value!r}; valid days: {', '.join(days)}")


def now_local(config: EventConfig) -> datetime:
    return datetime.now(config.tz).replace(tzinfo=None)


def _contains(hay: str, needle: str) -> bool:
    return needle.lower() in hay.lower()


def _score(s: Session, terms: list[str]) -> int | None:
    """Return a relevance score, or None if any term is missing everywhere."""
    score = 0
    speakers = " ".join(f"{sp.name} {sp.company}" for sp in s.speakers)
    tags = " ".join((s.track, *s.topics, *s.projects, *s.tags))
    for t in terms:
        if _contains(s.title, t):
            score += 5
        elif _contains(tags, t):
            score += 3
        elif _contains(speakers, t):
            score += 2
        elif _contains(s.abstract, t):
            score += 1
        else:
            return None
    if s.is_plenum:
        score += 1  # keynotes are what people usually mean
    return score


def search(agenda: Agenda, q: Query) -> list[Session]:
    terms = [t for t in (q.text or "").split() if t]
    day = resolve_day(q.day, agenda)
    hits: list[tuple[int, Session]] = []
    for s in agenda.sessions:
        if not terms and not q.include_breaks and not s.is_talk:
            continue
        if not q.include_offsite and s.is_offsite:
            continue
        if day and s.day != day:
            continue
        t = s.start.time()
        if q.after and t < q.after:
            continue
        if q.before and t >= q.before:
            continue
        if q.room and not _contains(s.room, q.room):
            continue
        if q.track and not _contains(" ".join((s.track, *s.topics, *s.tags)), q.track):
            continue
        if q.project and not any(_contains(p, q.project) for p in s.projects):
            continue
        if q.level and not _contains(s.level, q.level):
            continue
        if q.speaker and not any(sp.matches(q.speaker) for sp in s.speakers):
            continue
        score = _score(s, terms) if terms else 0
        if score is None:
            continue
        hits.append((score, s))
    hits.sort(key=lambda h: (-h[0], h[1].start, h[1].room))
    return [s for _, s in hits]


@dataclass
class WhatsOn:
    running: list[Session]  # talks in progress, shortest first
    upcoming: list[Session]  # talks starting within the window
    blocks: list[Session]  # all-day umbrellas in progress (co-located days, showcase floor)
    offsite: list[Session]  # off-site sponsor events in progress


def whats_on(agenda: Agenda, at: datetime, lookahead_minutes: int = 60) -> WhatsOn:
    """Split what is happening at `at` into talks, all-day blocks and off-site events."""
    horizon = at + timedelta(minutes=lookahead_minutes)
    live = [s for s in agenda.sessions if s.start <= at < s.end and s.is_talk]
    offsite = [s for s in live if s.is_offsite]
    blocks = [s for s in live if s.is_block and not s.is_offsite]
    running = sorted((s for s in live if not s.is_block and not s.is_offsite), key=lambda s: (s.start, s.room))
    upcoming = [s for s in agenda.sessions if at < s.start <= horizon and s.is_talk and not s.is_block and not s.is_offsite]
    return WhatsOn(running, upcoming, blocks, offsite)


@dataclass
class AgendaIssue:
    kind: str  # "overlap" | "tight" | "missing"
    detail: str


def check_agenda(
    agenda: Agenda, ids: list[str], min_gap_minutes: int = 10
) -> tuple[list[Session], list[AgendaIssue]]:
    """Order the picked sessions and flag overlaps and venue changes with no walking time."""
    picked: list[Session] = []
    issues: list[AgendaIssue] = []
    for sid in ids:
        s = agenda.by_id(sid.strip())
        if s is None:
            issues.append(AgendaIssue("missing", f"no session with id {sid}"))
        else:
            picked.append(s)
    picked.sort(key=lambda s: s.start)
    for a, b in zip(picked, picked[1:]):
        if a.overlaps(b):
            issues.append(AgendaIssue("overlap", f"[{a.id}] {a.title!r} overlaps [{b.id}] {b.title!r}"))
            continue
        gap = int((b.start - a.end).total_seconds() // 60)
        if a.venue and b.venue and a.venue != b.venue and gap < min_gap_minutes:
            issues.append(
                AgendaIssue(
                    "tight",
                    f"only {gap} min to get from {a.venue} to {b.venue} between [{a.id}] and [{b.id}]",
                )
            )
    return picked, issues


def find_speakers(agenda: Agenda, needle: str, limit: int = 10) -> list[Speaker]:
    n = needle.strip()
    if not n:
        return []
    exact = [sp for sp in agenda.speakers if sp.name.lower() == n.lower()]
    rest = [sp for sp in agenda.speakers if sp.matches(n) and sp not in exact]
    return (exact + rest)[:limit]


def sessions_of(agenda: Agenda, sp: Speaker) -> list[Session]:
    return [s for s in agenda.sessions if any(x.id == sp.id for x in s.speakers)]


# --- formatting -------------------------------------------------------------


def fmt_when(s: Session) -> str:
    return f"{s.start:%a %d %b %H:%M}-{s.end:%H:%M}"


def fmt_line(s: Session) -> str:
    who = ", ".join(sp.name for sp in s.speakers)
    parts = [f"[{s.id}]", fmt_when(s), s.room or "-", s.track or "-", s.title]
    line = " | ".join(parts)
    if who:
        line += f" — {who}"
    if s.access:
        line += f" [{s.access.lower()}]"
    return line


def fmt_lines(sessions: list[Session], limit: int | None = None) -> str:
    if not sessions:
        return "No matching sessions."
    total = len(sessions)
    shown = sessions[:limit] if limit else sessions
    head = f"{len(shown)} of {total} sessions" if len(shown) < total else f"{total} sessions"
    return head + ":\n" + "\n".join(fmt_line(s) for s in shown)


def fmt_detail(s: Session) -> str:
    out = [
        f"# {s.title}",
        f"id: {s.id}",
        f"when: {fmt_when(s)} ({s.minutes} min, event local time)",
        f"where: {s.room or 'tba'}",
    ]
    if s.track:
        out.append(f"track: {s.track}")
    if s.program:
        out.append(f"program: {s.program}")
    if s.access:
        out.append(f"access: {s.access}")
    if s.level:
        out.append(f"level: {s.level}")
    if s.topics:
        out.append(f"topics: {', '.join(s.topics)}")
    if s.projects:
        out.append(f"projects: {', '.join(s.projects)}")
    if s.tags:
        out.append(f"tags: {', '.join(s.tags)}")
    for k, v in s.links.items():
        out.append(f"{k}: {v}")
    if s.speakers:
        out.append("speakers:")
        for sp in s.speakers:
            who = sp.name + (f" ({sp.tagline})" if sp.tagline else "")
            out.append(f"  - {who}")
    if s.abstract:
        out.append("")
        out.append(s.abstract)
    return "\n".join(out)


def fmt_speaker(sp: Speaker, sessions: list[Session]) -> str:
    out = [f"# {sp.name}"]
    if sp.tagline:
        out.append(sp.tagline)
    for k, v in sp.links.items():
        out.append(f"{k}: {v}")
    if sessions:
        out.append("sessions:")
        out.extend(f"  {fmt_line(s)}" for s in sessions)
    if sp.bio:
        out.append("")
        out.append(sp.bio)
    return "\n".join(out)


def describe(agenda: Agenda, config: EventConfig) -> str:
    talks = [s for s in agenda.sessions if s.is_talk]
    day_labels = [f"{d} ({datetime.fromisoformat(d):%a})" for d in event_days(agenda)]
    rooms = sorted({s.room for s in talks if s.room and not s.is_offsite})
    projects = sorted({p for s in talks for p in s.projects})
    now = now_local(config)
    out = [
        f"{config.name}, {config.city}. Times are {config.timezone} local; now there it is {now:%a %d %b %H:%M}.",
        f"{len(talks)} sessions, {len(agenda.speakers)} speakers, {len(rooms)} rooms.",
        f"Days: {', '.join(day_labels)}",
    ]
    for program in agenda.programs or ("",):
        subset = [s for s in talks if s.program == program]
        tracks = sorted({s.track for s in subset if s.track})
        label = program or "Tracks"
        out.append(f"{label} ({len(subset)} sessions). Tracks: {'; '.join(tracks)}")
    out.append(f"Rooms: {'; '.join(rooms)}")
    if projects:
        out.append(f"CNCF projects tagged: {', '.join(projects)}")
    out.append(f"Official schedule: {config.official_url}")
    return "\n".join(out)
