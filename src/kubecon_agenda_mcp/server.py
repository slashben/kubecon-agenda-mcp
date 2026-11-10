"""MCP server exposing the conference agenda to AI agents.

Runs as a stateless Streamable HTTP server so it can live behind any
scale-to-zero function/container platform (Cloud Run, Lambda, Fly...).
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
import os
from datetime import datetime

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import __version__, agenda as ag
from .config import EventConfig, load_config
from .feed import AgendaStore

log = logging.getLogger("kubecon_agenda_mcp")

REPO_URL = "https://github.com/slashben/kubecon-agenda-mcp"

# Salt for the per-client hash in tool-call logs. The hash lets us count distinct
# clients without storing IPs or user agents; without a salt the hash is still
# one-way but guessable, so deployments should set LOG_SALT.
LOG_SALT = os.environ.get("LOG_SALT", "")


def client_fields(ctx: Context | None) -> dict[str, object]:
    """Anonymous client identity for log lines: a salted hash of IP + user agent,
    the coarse agent family and the MCP session id when the client sends one.
    No raw IP or user agent is ever logged."""
    out: dict[str, object] = {}
    if ctx is None:
        return out
    try:
        headers = ctx.headers or {}
    except Exception:  # noqa: BLE001 - no HTTP request behind this call (tests, stdio)
        return out
    ip = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ua = headers.get("user-agent", "")
    out["client"] = hashlib.sha256(f"{LOG_SALT}|{ip}|{ua}".encode()).hexdigest()[:12]
    # Coarse agent family only (e.g. "claude-code", "Claude-User", "python-httpx").
    out["agent"] = re.split(r"[/ (]", ua, 1)[0][:40] if ua else None
    if sid := headers.get("mcp-session-id"):
        out["session"] = sid[:16]
    return out


def build_server(config: EventConfig | None = None, store: AgendaStore | None = None) -> MCPServer:
    config = config or load_config()
    store = store or AgendaStore(config)

    mcp = MCPServer(
        name="kubecon-agenda",
        title=f"{config.short_name} agenda",
        version=__version__,
        website_url=REPO_URL,
        instructions=(
            f"Community-run, read-only agenda for {config.name} ({config.city}). Covers the main "
            "conference, every talk of the CNCF-hosted co-located events and the Maintainer Summit. "
            "Not affiliated with CNCF or the Linux Foundation. "
            f"All times are {config.timezone} local. Start with describe_event to learn the valid days, "
            "tracks and rooms, then search_sessions. Session ids from search results feed get_session and "
            f"check_agenda. Official schedule: {config.official_url}"
        ),
    )

    def _log(tool: str, ctx: Context | None = None, **fields: object) -> None:
        # One JSON line per call for log-based metrics. Query text is never logged;
        # the client is identified only by a salted hash (see client_fields).
        log.info(json.dumps({"event": "tool_call", "tool": tool, **client_fields(ctx), **fields}))

    @mcp.tool(description="Event overview: name, local time right now, valid days, tracks, rooms and CNCF project tags. Call this first.")
    async def describe_event(ctx: Context) -> str:
        _log("describe_event", ctx)
        return ag.describe(await store.get(), config)

    @mcp.tool(
        description=(
            "Search sessions. Filters combine with AND; results are ranked by relevance (title > tags > speaker > abstract) "
            "then by time. One line per session: [id] | when | room | track | title — speakers. "
            "Breaks, registration and social events (receptions, group photos) are hidden unless include_breaks is true."
        )
    )
    async def search_sessions(
        query: str | None = None,
        speaker: str | None = None,
        day: str | None = None,
        after: str | None = None,
        before: str | None = None,
        room: str | None = None,
        track: str | None = None,
        project: str | None = None,
        level: str | None = None,
        include_breaks: bool = False,
        include_offsite: bool = True,
        limit: int = 25,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> str:
        """
        Args:
            query: Words that must all appear somewhere in the session (title, tags, speakers or abstract).
            speaker: Speaker name or company substring.
            day: Weekday (mon..thu), day of month (10) or ISO date (2026-11-10).
            after: Starts at or after this local time, e.g. 14:00 or 2pm.
            before: Starts before this local time, e.g. 12:00 or noon as 12pm.
            room: Room or venue substring, e.g. "Grand Ballroom H" or "Hyatt".
            track: Track, topic or tag substring, e.g. "Security", "Istio Day", "Co-located".
            project: CNCF project tag substring, e.g. "Cilium", "Falco", "Kubeflow".
            level: Audience level: Beginner, Advanced or Any.
            include_breaks: Also return breaks, lunches, registration and social events (receptions, group photos).
            include_offsite: Set false to hide off-site sponsor events (separate registration, not at the venue).
            limit: Max lines to return (1-100).
        """
        agenda = await store.get()
        try:
            q = ag.Query(
                text=query,
                speaker=speaker,
                day=day,
                after=ag.parse_time(after),
                before=ag.parse_time(before),
                room=room,
                track=track,
                project=project,
                level=level,
                include_breaks=include_breaks,
                include_offsite=include_offsite,
            )
            hits = ag.search(agenda, q)
        except ValueError as e:
            _log("search_sessions", ctx, rejected=True)
            return f"Bad filter: {e}"
        used = [k for k, v in vars(q).items() if v not in (None, False, "")]
        _log("search_sessions", ctx, filters=used, matches=len(hits))
        return ag.fmt_lines(hits, limit=max(1, min(limit, 100)))

    @mcp.tool(description="Full details for one session id: abstract, speakers, room, time, level, project tags and any slides/repo links.")
    async def get_session(id: str, ctx: Context = None) -> str:  # type: ignore[assignment]
        s = (await store.get()).by_id(id.strip())
        _log("get_session", ctx, found=s is not None)
        return ag.fmt_detail(s) if s else f"No session with id {id}."

    @mcp.tool(
        description=(
            "What is happening right now (event local time) and what starts within the next hour, "
            "split into talks in progress, talks starting soon, all-day blocks in progress (co-located days, "
            "showcase floor) and off-site sponsor events. Pass `at` (e.g. 'tue 14:30' or '2026-11-10 14:30') "
            "to ask about another moment."
        )
    )
    async def whats_on(at: str | None = None, lookahead_minutes: int = 60, ctx: Context = None) -> str:  # type: ignore[assignment]
        agenda = await store.get()
        try:
            when = _parse_moment(at, agenda, config)
        except ValueError as e:
            _log("whats_on", ctx, rejected=True)
            return f"Bad time: {e}"
        w = ag.whats_on(agenda, when, lookahead_minutes)
        _log("whats_on", ctx, running=len(w.running), upcoming=len(w.upcoming), blocks=len(w.blocks))

        def section(title: str, items: list) -> list[str]:
            lines = [f"{title} ({len(items)}):"]
            lines.extend(ag.fmt_line(s) for s in items) if items else lines.append("  nothing")
            return lines

        out = [f"At {when:%a %d %b %H:%M} ({config.timezone}):"]
        out += section("Talks in progress", w.running)
        out += section(f"Talks starting within {lookahead_minutes} min", w.upcoming)
        if w.blocks:
            out.append(f"All-day blocks in progress ({len(w.blocks)}): " + "; ".join(f"{b.title} [{b.id}] ({b.room})" for b in w.blocks))
        if w.offsite:
            out.append(f"Off-site sponsor events in progress: {len(w.offsite)} (search with track='Sponsor-hosted' for details)")
        return "\n".join(out)

    @mcp.tool(description="Find speakers by name or company and list their sessions.")
    async def find_speaker(name: str, limit: int = 5, ctx: Context = None) -> str:  # type: ignore[assignment]
        agenda = await store.get()
        found = ag.find_speakers(agenda, name, limit=max(1, min(limit, 20)))
        _log("find_speaker", ctx, matches=len(found))
        if not found:
            return f"No speaker matching {name!r}."
        return "\n\n".join(ag.fmt_speaker(sp, ag.sessions_of(agenda, sp)) for sp in found)

    @mcp.tool(
        description=(
            "Validate a personal agenda: pass the session ids you plan to attend. Returns them in time order and "
            "flags every overlapping pair, duplicates, unknown ids, and venue changes (e.g. Salt Palace -> Hyatt) "
            "with too little walking time."
        )
    )
    async def check_agenda(session_ids: list[str], min_gap_minutes: int = 10, ctx: Context = None) -> str:  # type: ignore[assignment]
        if not session_ids:
            return "No session ids given. Pass the ids from search_sessions results."
        agenda = await store.get()
        picked, issues = ag.check_agenda(agenda, session_ids, min_gap_minutes)
        _log("check_agenda", ctx, picked=len(picked), issues=len(issues))
        out = [ag.fmt_lines(picked)]
        if issues:
            out.append("Issues:")
            out.extend(f"  {i.kind}: {i.detail}" for i in issues)
        else:
            out.append("No conflicts.")
        return "\n".join(out)

    @mcp.resource("agenda://day/{day}", description="Every talk on one day (ISO date or weekday), in time order.")
    async def day_resource(day: str) -> str:
        agenda = await store.get()
        return ag.fmt_lines(ag.search(agenda, ag.Query(day=day)))

    @mcp.prompt(description="Plan a conference day around a set of interests.")
    def plan_my_day(interests: str, day: str = "tue") -> str:
        return (
            f"You are planning my {config.short_name} day for {day}. My interests: {interests}.\n"
            "1. Call describe_event to learn the day, tracks and rooms.\n"
            "2. Run a few search_sessions calls for my interests on that day.\n"
            "3. Pick a non-overlapping set of 5-7 sessions; call check_agenda with their ids.\n"
            "4. Present the plan as a timeline with room and one line on why each pick."
        )

    @mcp.custom_route("/", methods=["GET"])
    async def landing(request: Request) -> PlainTextResponse:
        base = str(request.base_url).rstrip("/")
        return PlainTextResponse(
            f"{config.short_name} agenda MCP server (community-run, read-only).\n"
            f"MCP endpoint (Streamable HTTP): {base}/mcp\n"
            f"Official schedule: {config.official_url}\n"
            f"Source: {REPO_URL}\n"
        )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        agenda = await store.get()
        return JSONResponse({"ok": True, "sessions": len(agenda.sessions), "version": __version__})

    return mcp


def _parse_moment(value: str | None, agenda, config: EventConfig) -> datetime:
    if not value or not value.strip():
        return ag.now_local(config)
    parts = value.strip().split()
    if len(parts) == 1:
        # just a time -> today in event time; just a day -> 09:00 that day
        try:
            t = ag.parse_time(parts[0])
            return datetime.combine(ag.now_local(config).date(), t)
        except ValueError:
            d = ag.resolve_day(parts[0], agenda)
            return datetime.fromisoformat(f"{d}T09:00")
    d = ag.resolve_day(parts[0], agenda)
    t = ag.parse_time(" ".join(parts[1:]))
    return datetime.combine(datetime.fromisoformat(d).date(), t)


def create_app():
    """ASGI entry point (uvicorn kubecon_agenda_mcp.server:app)."""
    mcp = build_server()
    return mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


app = None  # populated lazily by uvicorn factory usage: `uvicorn kubecon_agenda_mcp.server:create_app --factory`
