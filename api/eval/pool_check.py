"""Pre-draft pool health gate.

The 2026-08 audit found that the most dangerous bug class manifests as
silent absence or staleness: a cut player still in the pool, a top-ADP name
missing entirely, a placeholder ADP cluster masquerading as market data, a
namesake swallowing another player's identity, a board built from weeks-old
data. None of those can trip an engine invariant -- the engine never hears
about what is missing. This gate checks the inputs themselves, runs inside
rehearsal and the pre-draft board build, and surfaces problems as panel
warnings rather than log lines.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.engine.vor import PoolPlayer
from api.ingest.players import make_merge_name
from api.ingest.projections import placeholder_adp_values
from api.models import Adp, PlayerIds, Projection
from api.schemas import LeagueSettings

TOP_N_ADP = 120
MAX_DATA_AGE = timedelta(days=7)


def _as_utc(timestamp: datetime | None) -> datetime | None:
    if timestamp is None:
        return None
    return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)


def pool_health_problems(
    session: Session,
    settings: LeagueSettings,
    pool: list[PoolPlayer],
    *,
    top_n: int = TOP_N_ADP,
    max_age: timedelta = MAX_DATA_AGE,
    live_team_count: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Input problems that make draft advice untrustworthy, in plain English."""
    problems: list[str] = []
    now = now or datetime.now(UTC)

    # 1. Every top-N market name must exist in the pool with a projection. A
    # missing one means an ingest hole (identity lost, projection dropped,
    # or a player wrongly filtered) exactly where picks are decided.
    pool_ids = {p.canonical_id for p in pool}
    top_market = session.execute(
        select(Adp.canonical_id, func.min(Adp.value).label("best"))
        .group_by(Adp.canonical_id)
        .order_by(func.min(Adp.value))
        .limit(top_n)
    ).all()
    missing = [cid for cid, _ in top_market if str(cid) not in pool_ids]
    if missing:
        names: dict[int, str] = {
            cid: name
            for cid, name in session.execute(
                select(PlayerIds.canonical_id, PlayerIds.full_name).where(
                    PlayerIds.canonical_id.in_(missing)
                )
            ).tuples()
        }
        listed = ", ".join(str(names.get(cid, cid)) for cid in missing[:8])
        problems.append(
            f"{len(missing)} of the top {top_n} ADP players are missing from the "
            f"pool: {listed}"
        )

    # 2. Namesakes inside the pool: two players sharing merge-name+position
    # means name-based joins are refusing (correct) or worse, historically
    # merged identities. Either way a human should know before drafting.
    name_counts = Counter((make_merge_name(p.name), p.position) for p in pool)
    dupes = sorted(name for (name, _), count in name_counts.items() if count > 1)
    if dupes:
        problems.append(
            "namesake collision(s) in the pool -- verify the right player is "
            f"being drafted: {', '.join(dupes[:5])}"
        )

    # 3. Data age. fetched_at was previously read by nothing: a dead scheduler
    # meant drafting on a weeks-old board with full confidence.
    newest_projection = _as_utc(
        session.scalar(select(func.max(Projection.fetched_at)).where(Projection.week.is_(None)))
    )
    newest_adp = _as_utc(session.scalar(select(func.max(Adp.fetched_at))))
    for label, newest in (("projections", newest_projection), ("ADP", newest_adp)):
        if newest is None:
            problems.append(f"no {label} in the database")
        elif now - newest > max_age:
            problems.append(
                f"{label} were last ingested {(now - newest).days} days ago -- "
                "re-run ingest before drafting"
            )

    # 4. Live room size vs stored settings (teams leave leagues; a 16-team
    # league quietly became 14 and nothing compared the two).
    if live_team_count is not None and live_team_count != settings.total_teams:
        problems.append(
            f"live room has {live_team_count} teams but stored settings say "
            f"{settings.total_teams}"
        )

    # 5. Placeholder-ADP clusters per source. The ingest guard nulls ESPN's on
    # write; this is the tripwire for the same pattern appearing anywhere.
    for source in ("espn", "sleeper", "ffc"):
        values = session.scalars(select(Adp.value).where(Adp.source == source)).all()
        cluster = placeholder_adp_values([float(v) for v in values])
        if cluster:
            count = sum(1 for v in values if round(float(v)) in cluster)
            problems.append(
                f"{source} ADP has a placeholder-sized cluster: {count} players "
                f"at ~{min(cluster)} -- those values are not market data"
            )

    return problems
