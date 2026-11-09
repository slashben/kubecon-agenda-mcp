import json
from datetime import datetime, time
from pathlib import Path

import pytest

from kubecon_agenda_mcp import agenda as ag
from kubecon_agenda_mcp.config import EventConfig, parse_feeds
from kubecon_agenda_mcp.feed import AgendaStore, normalize, split_access

HERE = Path(__file__).parent
MAIN = json.loads((HERE / "fixture_feed.json").read_text())
COLOCATED = json.loads((HERE / "fixture_colocated.json").read_text())
CONFIG = EventConfig(
    name="Test Con",
    short_name="TestCon",
    feeds=parse_feeds("main=Main conference,colo=Co-located events"),
    timezone="America/Denver",
    official_url="https://example.org",
    city="Salt Lake City",
    cache_ttl=60,
)


@pytest.fixture
def agenda():
    return AgendaStore(CONFIG).load_static((MAIN, "Main conference"), (COLOCATED, "Co-located events"))


def test_normalize_joins_rooms_speakers_and_categories(agenda):
    s = agenda.by_id("1236746")
    assert s is not None
    assert s.title.startswith("Gone in 60 Minutes")
    assert s.room == "Hyatt Regency, Level 2, Salt Lake Ballroom DE"
    assert s.venue == "Hyatt Regency"
    assert s.track == "Security"
    assert s.program == "Main conference"
    assert s.start == datetime(2026, 11, 10, 14, 50)
    assert [sp.name for sp in s.speakers] == ["Ben Hirschberg", "Dr. Constanze Roedig"]
    assert s.speakers[0].company


def test_sessions_sorted_by_time(agenda):
    starts = [s.start for s in agenda.sessions]
    assert starts == sorted(starts)


def test_search_ranks_title_over_abstract(agenda):
    hits = ag.search(agenda, ag.Query(text="detection"))
    assert hits
    assert "detection" in hits[0].title.lower()


def test_search_is_and_over_terms(agenda):
    assert ag.search(agenda, ag.Query(text="detection zzzznotaword")) == []


def test_search_filters_combine(agenda):
    hits = ag.search(agenda, ag.Query(day="tue", after=time(14, 0), room="Hyatt", track="Security"))
    assert hits and all(h.day == "2026-11-10" and h.start.time() >= time(14, 0) for h in hits)
    assert all("Hyatt" in h.room for h in hits)


def test_colocated_individual_talks_are_searchable(agenda):
    hits = ag.search(agenda, ag.Query(day="mon", track="Agentics Day"))
    assert hits
    talks = [h for h in hits if not h.is_block]
    assert talks, "expected per-talk entries from the co-located feed, not just the day block"
    assert all(h.program == "Co-located events" for h in talks)
    assert all(h.is_colocated for h in talks)


def test_main_feed_day_block_is_flagged_as_block(agenda):
    block = agenda.by_id("1289780")  # Agentics Day umbrella from the main feed
    assert block.is_block and block.is_colocated
    assert block.access == "ALL ACCESS PASS REQUIRED"
    assert "|" not in block.title


@pytest.mark.parametrize(
    "raw,title,access",
    [
        ("ArgoCon Hosted by CNCF - Full Day Event | ALL ACCESS PASS REQUIRED", "ArgoCon Hosted by CNCF - Full Day Event", "ALL ACCESS PASS REQUIRED"),
        ("Azure Day | SEPARATE REGISTRATION REQUIRED", "Azure Day", "SEPARATE REGISTRATION REQUIRED"),
        ("NVIDIA Workshop | SOLD OUT", "NVIDIA Workshop", "SOLD OUT"),
        ("Kernel | Kubectl: a talk about pipes", "Kernel | Kubectl: a talk about pipes", ""),
    ],
)
def test_split_access(raw, title, access):
    assert split_access(raw) == (title, access)


def test_breaks_hidden_unless_requested(agenda):
    no_breaks = ag.search(agenda, ag.Query(day="tue"))
    with_breaks = ag.search(agenda, ag.Query(day="tue", include_breaks=True))
    assert all(s.is_talk for s in no_breaks)
    assert len(with_breaks) >= len(no_breaks)


def test_offsite_can_be_excluded(agenda):
    assert not any(s.is_offsite for s in ag.search(agenda, ag.Query(include_offsite=False)))


@pytest.mark.parametrize(
    "raw,expected",
    [("14:00", time(14, 0)), ("2pm", time(14, 0)), ("9:30am", time(9, 30)), ("12pm", time(12, 0)), ("12am", time(0, 0)), ("9", time(9, 0))],
)
def test_parse_time(raw, expected):
    assert ag.parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "13pm", "noon", "2:60"])
def test_parse_time_rejects(raw):
    with pytest.raises(ValueError):
        ag.parse_time(raw)


def test_resolve_day_variants(agenda):
    assert ag.resolve_day("tue", agenda) == "2026-11-10"
    assert ag.resolve_day("Tuesday", agenda) == "2026-11-10"
    assert ag.resolve_day("10", agenda) == "2026-11-10"
    assert ag.resolve_day("11-10", agenda) == "2026-11-10"
    with pytest.raises(ValueError):
        ag.resolve_day("fri", agenda)


def test_whats_on_separates_talks_from_blocks(agenda):
    w = ag.whats_on(agenda, datetime(2026, 11, 9, 9, 20), 60)
    assert any(s.program == "Co-located events" for s in w.running), "co-located talk should be in progress"
    assert all(not s.is_block for s in w.running)
    assert any(s.id == "1289780" for s in w.blocks), "Agentics Day umbrella belongs in blocks"
    assert all(datetime(2026, 11, 9, 9, 20) < s.start <= datetime(2026, 11, 9, 10, 20) for s in w.upcoming)


def test_whats_on_tuesday(agenda):
    w = ag.whats_on(agenda, datetime(2026, 11, 10, 15, 0), 60)
    assert any(s.id == "1236746" for s in w.running)


def test_check_agenda_flags_overlap_and_missing(agenda):
    a = agenda.by_id("1236746")
    overlapping = next(s for s in agenda.sessions if s.id != a.id and s.overlaps(a))
    picked, issues = ag.check_agenda(agenda, [a.id, overlapping.id, "nope"])
    kinds = {i.kind for i in issues}
    assert {"overlap", "missing"} <= kinds
    assert [s.id for s in picked] == sorted([a.id, overlapping.id], key=lambda i: agenda.by_id(i).start)


def test_check_agenda_flags_tight_venue_change(agenda):
    a = agenda.by_id("1236746")  # Hyatt, ends 15:20
    nxt = next(s for s in agenda.sessions if s.venue == "Salt Palace" and s.start >= a.end)
    gap = int((nxt.start - a.end).total_seconds() // 60)
    _, issues = ag.check_agenda(agenda, [a.id, nxt.id], min_gap_minutes=gap + 1)
    assert any(i.kind == "tight" for i in issues)
    _, issues = ag.check_agenda(agenda, [a.id, nxt.id], min_gap_minutes=gap)
    assert not issues


def test_find_speaker_by_name_and_company(agenda):
    assert ag.find_speakers(agenda, "hirschberg")[0].name == "Ben Hirschberg"
    sp = ag.find_speakers(agenda, "Ben Hirschberg")[0]
    assert [s.id for s in ag.sessions_of(agenda, sp)] == ["1236746"]


def test_format_line_is_single_line(agenda):
    for s in agenda.sessions:
        assert "\n" not in ag.fmt_line(s)


def test_describe_lists_programs(agenda):
    text = ag.describe(agenda, CONFIG)
    assert "2026-11-10 (Tue)" in text
    assert "Main conference (" in text and "Co-located events (" in text
    assert "Agentics Day: MCP + Agents" in text


def test_parse_feeds():
    feeds = parse_feeds("abc=Main, def = Colo ,ghi")
    assert [(f.sessionize_id, f.program) for f in feeds] == [("abc", "Main"), ("def", "Colo"), ("ghi", "ghi")]


def test_normalize_handles_empty_feed():
    assert normalize({}) == ([], [])
