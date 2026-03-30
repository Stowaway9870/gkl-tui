"""Player Explorer — ownership timeline, usage analysis, and stat breakdowns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from gkl.yahoo_api import PlayerStats, Transaction, YahooFantasyAPI


@dataclass
class OwnershipStint:
    """A period where a player was on a specific fantasy team."""
    team_key: str
    team_name: str
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD or "" if still owned
    weeks: list[int] = field(default_factory=list)
    # Per-week roster data: week -> (selected_position, stats_dict)
    week_data: dict[int, tuple[str, dict[str, str]]] = field(default_factory=dict)

    @property
    def days(self) -> int:
        start = datetime.strptime(self.start_date, "%Y-%m-%d")
        end_str = self.end_date or datetime.now().strftime("%Y-%m-%d")
        end = datetime.strptime(end_str, "%Y-%m-%d")
        return max(1, (end - start).days + 1)

    @property
    def date_range_str(self) -> str:
        start = datetime.strptime(self.start_date, "%Y-%m-%d")
        s = start.strftime("%b %d")
        if self.end_date:
            end = datetime.strptime(self.end_date, "%Y-%m-%d")
            return f"{s} - {end.strftime('%b %d')}"
        return f"{s} - present"


@dataclass
class UsageSummary:
    """Aggregate usage breakdown for a player's season."""
    total_days: int = 0
    started_days: int = 0
    benched_days: int = 0
    il_days: int = 0
    not_owned_days: int = 0
    # Stats aggregated by usage type
    started_stats: dict[str, str] = field(default_factory=dict)
    benched_stats: dict[str, str] = field(default_factory=dict)
    il_stats: dict[str, str] = field(default_factory=dict)
    not_owned_stats: dict[str, str] = field(default_factory=dict)


@dataclass
class StintStats:
    """Stats for one ownership stint, split by started/benched."""
    total_stats: dict[str, str] = field(default_factory=dict)
    started_stats: dict[str, str] = field(default_factory=dict)
    benched_stats: dict[str, str] = field(default_factory=dict)
    total_days: int = 0
    started_days: int = 0
    benched_days: int = 0


# Positions that count as "started" (not bench/IL)
_ACTIVE_POSITIONS = {
    "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "Util", "DH",
    "SP", "RP", "P",
}
_IL_POSITIONS = {"IL", "IL+", "DL", "NA"}


def classify_position(selected_position: str) -> str:
    """Classify a roster position as started, benched, or IL."""
    if selected_position in _ACTIVE_POSITIONS:
        return "started"
    if selected_position in _IL_POSITIONS:
        return "il"
    if selected_position == "BN":
        return "benched"
    return "benched"  # fallback


def build_ownership_timeline(
    player_key: str,
    transactions: list[Transaction],
    season_start: str = "",
) -> list[OwnershipStint]:
    """Build ownership timeline from transaction history.

    Returns list of OwnershipStints sorted chronologically.
    """
    # Collect all relevant transaction events for this player
    events: list[tuple[int, str, str, str]] = []  # (timestamp, action, team_key, team_name)

    for tx in transactions:
        for p in tx.players:
            if p.player_key != player_key:
                continue
            if p.action == "add" and p.to_team_key:
                events.append((tx.timestamp, "add", p.to_team_key, p.to_team))
            elif p.action == "drop" and p.from_team_key:
                events.append((tx.timestamp, "drop", p.from_team_key, p.from_team))

    # Sort by timestamp (oldest first)
    events.sort(key=lambda e: e[0])

    stints: list[OwnershipStint] = []
    current_team_key = ""
    current_team_name = ""
    current_start = ""

    for ts, action, team_key, team_name in events:
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

        if action == "add":
            current_team_key = team_key
            current_team_name = team_name
            current_start = date_str
        elif action == "drop":
            if current_team_key == team_key and current_start:
                stints.append(OwnershipStint(
                    team_key=team_key,
                    team_name=team_name,
                    start_date=current_start,
                    end_date=date_str,
                ))
            current_team_key = ""
            current_team_name = ""
            current_start = ""

    # If still owned, add open-ended stint
    if current_team_key and current_start:
        stints.append(OwnershipStint(
            team_key=current_team_key,
            team_name=current_team_name,
            start_date=current_start,
            end_date="",
        ))

    return stints


def map_weeks_to_stints(
    stints: list[OwnershipStint],
    week_dates: dict[int, tuple[str, str]],
) -> None:
    """Assign week numbers to each ownership stint based on date overlap."""
    for stint in stints:
        stint.weeks = []
        s_start = datetime.strptime(stint.start_date, "%Y-%m-%d")
        s_end_str = stint.end_date or datetime.now().strftime("%Y-%m-%d")
        s_end = datetime.strptime(s_end_str, "%Y-%m-%d")

        for week, (w_start_str, w_end_str) in week_dates.items():
            w_start = datetime.strptime(w_start_str, "%Y-%m-%d")
            w_end = datetime.strptime(w_end_str, "%Y-%m-%d")
            # Check if this week overlaps with the stint
            if s_start <= w_end and s_end >= w_start:
                stint.weeks.append(week)


def load_stint_roster_data(
    stints: list[OwnershipStint],
    api: YahooFantasyAPI,
    player_key: str,
) -> None:
    """For each stint, load the player's roster position and stats per week."""
    for stint in stints:
        for week in stint.weeks:
            try:
                players = api.get_roster_stats(stint.team_key, week)
                for p in players:
                    if p.player_key == player_key:
                        stint.week_data[week] = (p.selected_position, p.stats)
                        break
            except Exception:
                continue


def compute_stint_stats(
    stint: OwnershipStint,
    batting_stat_ids: list[str],
) -> StintStats:
    """Compute aggregated stats for a stint, split by started/benched."""
    result = StintStats()

    for week, (sel_pos, stats) in stint.week_data.items():
        usage = classify_position(sel_pos)
        if usage == "started":
            result.started_days += 1
            _add_stats(result.started_stats, stats, batting_stat_ids)
        elif usage == "benched":
            result.benched_days += 1
            _add_stats(result.benched_stats, stats, batting_stat_ids)
        _add_stats(result.total_stats, stats, batting_stat_ids)

    result.total_days = stint.days

    return result


def compute_usage_summary(
    stints: list[OwnershipStint],
    season_total_days: int,
    batting_stat_ids: list[str],
) -> UsageSummary:
    """Compute the season-wide usage summary across all stints."""
    summary = UsageSummary(total_days=season_total_days)

    for stint in stints:
        for week, (sel_pos, stats) in stint.week_data.items():
            usage = classify_position(sel_pos)
            if usage == "started":
                summary.started_days += 1
                _add_stats(summary.started_stats, stats, batting_stat_ids)
            elif usage == "benched":
                summary.benched_days += 1
                _add_stats(summary.benched_stats, stats, batting_stat_ids)
            elif usage == "il":
                summary.il_days += 1
                _add_stats(summary.il_stats, stats, batting_stat_ids)

    owned_days = sum(s.days for s in stints)
    summary.not_owned_days = max(0, season_total_days - owned_days)

    return summary


def _add_stats(
    target: dict[str, str],
    source: dict[str, str],
    counting_ids: list[str],
) -> None:
    """Add counting stats from source into target accumulator."""
    for sid, val in source.items():
        if sid in ("3", "4", "5"):  # AVG, OBP, SLG - skip rate stats
            continue
        if "/" in val:  # H/AB format
            existing = target.get(sid, "0/0")
            e_parts = existing.split("/")
            v_parts = val.split("/")
            try:
                num = int(e_parts[0]) + int(v_parts[0])
                den = int(e_parts[1]) + int(v_parts[1])
                target[sid] = f"{num}/{den}"
            except (ValueError, IndexError):
                pass
        else:
            try:
                existing = int(target.get(sid, "0"))
                target[sid] = str(existing + int(val))
            except ValueError:
                pass
