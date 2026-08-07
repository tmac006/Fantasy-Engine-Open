"""Cross-cutting services: ID translation and pool assembly for the engine."""

import statistics
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.engine.scoring import score_stats
from api.engine.vor import PoolPlayer
from api.models import Adp, PlayerContext, PlayerIds, Projection, TeamContext
from api.schemas import LeagueSettings


def canonical_map_for_sleeper(session: Session, sleeper_ids: list[str]) -> dict[str, str]:
    if not sleeper_ids:
        return {}
    rows = session.execute(
        select(PlayerIds.sleeper_id, PlayerIds.canonical_id).where(
            PlayerIds.sleeper_id.in_(sleeper_ids)
        )
    ).all()
    return {str(sid): str(cid) for sid, cid in rows}


def canonical_map_for_espn(session: Session, espn_ids: list[str]) -> dict[str, str]:
    if not espn_ids:
        return {}
    rows = session.execute(
        select(PlayerIds.espn_id, PlayerIds.canonical_id).where(
            PlayerIds.espn_id.in_(espn_ids)
        )
    ).all()
    return {str(eid): str(cid) for eid, cid in rows}


def translate_espn_ids(session: Session, espn_ids: list[str]) -> tuple[list[str], list[str]]:
    """Translate ESPN-native IDs while preserving order and unknown picks."""
    mapping = canonical_map_for_espn(session, espn_ids)
    translated: list[str] = []
    unmapped: list[str] = []
    for espn_id in espn_ids:
        if espn_id in mapping:
            translated.append(mapping[espn_id])
        else:
            prefixed = f"espn:{espn_id}"
            translated.append(prefixed)
            unmapped.append(prefixed)
    return translated, unmapped


def translate_prefixed_ids(session: Session, prefixed: list[str]) -> tuple[list[str], list[str]]:
    """Map ['sleeper:123', ...] -> canonical ID strings.

    Unmapped IDs keep their platform prefix (never silently dropped — spec §6)
    and are returned separately so callers can log them loudly.
    """
    sleeper_ids = [p.removeprefix("sleeper:") for p in prefixed if p.startswith("sleeper:")]
    mapping = canonical_map_for_sleeper(session, sleeper_ids)
    out: list[str] = []
    unmapped: list[str] = []
    for p in prefixed:
        raw = p.removeprefix("sleeper:")
        if p.startswith("sleeper:") and raw in mapping:
            out.append(mapping[raw])
        else:
            out.append(p)
            unmapped.append(p)
    return out, unmapped


def build_pool(
    session: Session,
    settings: LeagueSettings,
    season: str = "2026",
    platform: str | None = None,
) -> list[PoolPlayer]:
    """Assemble the projected player pool, scored under this league's rules.

    Per player: score each source's projection (Sleeper raw stats through the
    league's scoring; ESPN's points as published), then take the median
    (spec §6: median, not mean).

    ADP precedence ends with the platform being drafted on: you are drafting
    against that room's market, so its ADP decides "will he last". (Learned
    the hard way: FFC had D'Andre Swift 20 picks earlier than Sleeper's own
    market, which made the engine recommend him a full round early.)
    """
    rows = session.execute(
        select(Projection, PlayerIds).join(
            PlayerIds, PlayerIds.canonical_id == Projection.canonical_id
        ).where(Projection.season == season, Projection.week.is_(None))
    ).all()
    player_context_rows = session.scalars(
        select(PlayerContext).where(PlayerContext.season == season)
    ).all()
    team_context_rows = session.scalars(
        select(TeamContext).where(TeamContext.season == season)
    ).all()

    contexts_by_player: dict[int, dict[str, PlayerContext]] = {}
    for context in player_context_rows:
        contexts_by_player.setdefault(context.canonical_id, {})[context.source] = context
    team_context_by_team = {
        context.team: context
        for context in team_context_rows
        if context.source == "nflverse"
    }

    reception_points = settings.scoring.get("rec", 0.0)
    adp_format = (
        "ppr"
        if reception_points >= 0.75
        else "half_ppr"
        if reception_points >= 0.25
        else "std"
    )
    adp_rows = session.scalars(
        select(Adp).where(Adp.format.in_({adp_format, "ppr"}))
    ).all()
    precedence = ["espn", "sleeper", "ffc"]
    if platform in ("sleeper", "espn"):
        precedence = [s for s in precedence if s != platform] + [platform]
    adp_by_player: dict[int, tuple[float, float | None]] = {}
    for source in precedence:  # lowest precedence first; later sources win
        source_rows = [
            a
            for a in adp_rows
            if a.source == source
            and (source != "ffc" or a.teams == settings.total_teams)
        ]
        by_format = {
            (a.canonical_id, a.format): (a.value, a.stdev)
            for a in source_rows
        }
        for canonical_id in {a.canonical_id for a in source_rows}:
            selected = by_format.get((canonical_id, adp_format)) or by_format.get(
                (canonical_id, "ppr")
            )
            if selected is not None:
                adp_by_player[canonical_id] = selected

    by_player: dict[int, list[float]] = {}
    meta: dict[int, PlayerIds] = {}
    for proj, ids in rows:
        if proj.source == "sleeper":
            if ids.position in ("K", "DEF"):
                # K/DEF stat keys are sparse and league scoring for them barely
                # varies; the source's own total is the reliable number.
                # (Scoring them through a mismatched key set is how a kicker
                # once topped the round-4 board.)
                total = proj.stats.get("pts_std", proj.points)
                if total is None:
                    continue
                points = float(total)
            else:
                points = score_stats(
                    proj.stats,
                    settings.scoring,
                    position=ids.position,
                )
        elif proj.points is not None:
            points = float(proj.points)
        else:
            continue
        # A season-long projection of zero (or less) is a source declining to
        # project the player, not a forecast that he scores nothing. ESPN
        # publishes 0.0 for most kickers, and folding those into the median
        # halved every real kicker (Tyler Bass: median(0, 103) = 51.5) while
        # leaving inflated one-source names on top -- which is why the engine
        # kept recommending the wrong kicker.
        if points <= 0.0:
            continue
        by_player.setdefault(ids.canonical_id, []).append(points)
        meta[ids.canonical_id] = ids

    pool: list[PoolPlayer] = []
    for canonical_id, source_points in by_player.items():
        ids = meta[canonical_id]
        if ids.position is None:
            continue
        adp = adp_by_player.get(canonical_id)
        platform_ids = {
            k: v
            for k, v in (("sleeper", ids.sleeper_id), ("espn", ids.espn_id))
            if v is not None
        }
        player_context = contexts_by_player.get(canonical_id, {})
        nflverse = player_context.get("nflverse")
        sleeper = player_context.get("sleeper")
        usage = _usage_context(ids.position, nflverse.data if nflverse else {})
        team_row = team_context_by_team.get(ids.team or "")
        team_data = _team_context(team_row.data if team_row else {})
        status, risks = _status_context(sleeper.data if sleeper else {})
        freshness = {
            source: _iso_timestamp(context.source_updated_at or context.fetched_at)
            for source, context in player_context.items()
        }
        if team_row is not None:
            freshness["team"] = _iso_timestamp(
                team_row.source_updated_at or team_row.fetched_at
            )
        consensus = statistics.median(source_points)
        has_range = len(source_points) >= 2
        pool.append(
            PoolPlayer(
                canonical_id=str(canonical_id),
                name=ids.full_name,
                position=ids.position,
                team=ids.team,
                points=consensus,
                adp=adp[0] if adp else None,
                adp_stdev=adp[1] if adp else None,
                projection_consensus=consensus,
                projection_low=min(source_points) if has_range else None,
                projection_high=max(source_points) if has_range else None,
                projection_stdev=statistics.pstdev(source_points) if has_range else None,
                usage=usage,
                team_context=team_data,
                status=status,
                risk_flags=tuple(risks),
                freshness=freshness,
                platform_ids=platform_ids,
            )
        )
    return pool


def _iso_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _nested(data: dict[str, Any], section: str, key: str) -> Any:
    value = data.get(section)
    return value.get(key) if isinstance(value, dict) else None


def _usage_context(position: str, data: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
    common = {
        "games": data.get("games"),
        "snap_pct": _nested(data, "snaps", "offense_pct"),
    }
    if position == "QB":
        selected = {
            "attempts": _nested(data, "volume", "attempts"),
            "carries": _nested(data, "volume", "carries"),
            "pass_epa_per_attempt": _nested(data, "efficiency", "passing_epa_per_attempt"),
            "cpoe": _nested(data, "efficiency", "passing_cpoe"),
        }
    elif position == "RB":
        selected = {
            "touches": _nested(data, "volume", "touches"),
            "carries": _nested(data, "volume", "carries"),
            "targets": _nested(data, "volume", "targets"),
            "target_share": _nested(data, "opportunity", "target_share"),
            "yards_per_carry": _nested(data, "efficiency", "yards_per_carry"),
        }
    else:
        selected = {
            "targets": _nested(data, "volume", "targets"),
            "receptions": _nested(data, "volume", "receptions"),
            "target_share": _nested(data, "opportunity", "target_share"),
            "air_yards_share": _nested(data, "opportunity", "air_yards_share"),
            "wopr": _nested(data, "opportunity", "wopr"),
            "yards_per_target": _nested(data, "efficiency", "yards_per_target"),
        }
    return {key: value for key, value in {**common, **selected}.items() if value is not None}


def _team_context(data: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
    offense = data.get("offense")
    if not isinstance(offense, dict):
        return {}
    keys = ("plays_per_game", "pass_rate", "yards_per_play", "epa_per_play", "touchdowns_per_game")
    return {key: offense[key] for key in keys if offense.get(key) is not None}


def _status_context(data: dict[str, Any]) -> tuple[str | None, list[str]]:
    status = str(data["status"]) if data.get("status") else None
    injury = str(data["injury_status"]) if data.get("injury_status") else None
    practice = (
        str(data["practice_participation"])
        if data.get("practice_participation")
        else None
    )
    parts = [part for part in (status, f"injury: {injury}" if injury else None) if part]
    risks: list[str] = []
    if injury and injury.lower() not in {"healthy", "none"}:
        risks.append(f"Sleeper injury designation: {injury}.")
    if practice:
        risks.append(f"Practice participation: {practice}.")
    return "; ".join(parts) or None, risks

