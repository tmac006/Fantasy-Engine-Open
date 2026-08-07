# Fantasy Football Draft and Season Assistant

## Purpose

Most fantasy football tools either cost money or tell you things you already
know. The paid ones mostly repackage a rankings list; the free ones mostly
repackage ADP. Neither answers the question you actually have on the clock,
which is not "who is the best player left" but "who is the best player left
*for my roster*, given what this specific room is about to take."

I wanted to build that myself, with real math, because I like football and I
wanted to see how far honest modeling could get. It is still a work in progress,
but it has held up well in testing so far.

Two rules shaped the whole project:

- **Measure, do not assume.** Every model here was tested against a real season.
  Several of them were rewritten when the data disagreed with the design. Those
  results are written down below, including the ones that were inconvenient.
- **Show the numbers, not a verdict.** Every recommendation explains itself in
  terms you can check against a box score, and says so when it is uncertain.

## What it does

- **Draft assistant.** A Chrome side panel that reads the live draft (ESPN and
  Sleeper), and recommends a pick with the reasoning attached. It gives two
  answers: the best pick for your roster, and the pick most likely to be gone if
  you wait. When they disagree, that disagreement is the useful part.
- **Waiver targets.** Ranks free agents in your league by what they are actually
  worth per game, with role changes and regression risk shown as context.
- **Start/sit.** Fills your lineup, adjusting for the betting market's view of
  each game, and flags genuine coin flips instead of pretending to be sure.
- **Web app.** News filtered to players who matter to you, league status, and
  the weekly views above.

Personal project, personal use. It reads public data and your own leagues; it is
not a product and is not hosted anywhere.

## How to run

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node 24 or
newer. No Docker required: the database is an embedded Postgres that runs in
user space.

    git clone <this repo>
    cd fantasy-football-engine-open
    cp .env.example .env

One command brings up everything, applies migrations, refreshes stale data, and
reports how current the market numbers are:

    ./scripts/draft-day.sh

The web app is then at `http://localhost:8000/app`.

To load the draft assistant in Chrome:

    cd extension && npm install && npm run build

Then open `chrome://extensions`, turn on Developer mode, choose "Load unpacked",
and select the `extension/` directory. Open a draft on ESPN or Sleeper and the
side panel picks it up automatically.

Running the tests:

    uv run pytest                      # engine, ingest, API
    cd extension && npm test           # extension logic

To watch the engine draft a full team against a simulated room and grade the
result:

    uv run python -m api.eval.mockdraft --slot 4 --seed 1

### Optional

ESPN private-league reads need your own session cookies in `.env` (`ESPN_S2`,
`ESPN_SWID`). Everything else works without them, including drafting, because
the browser extension uses your existing session.

## The math

### Value over replacement, not rankings

A player is worth the points he scores *above what you could get for free at his
position*. That replacement level is derived from your league's actual settings
rather than a generic list: with twelve teams and two running back slots plus a
flex, roughly the 29th running back is replaceable, so the 5th is worth his
points minus that baseline, not his raw total.

This is why the engine will pass on a higher-scoring quarterback for a lower-
scoring running back. Quarterbacks score more points, but the gap between the
best and the freely available one is small; at running back it is large.

### Will he last until my next pick?

Every pick is really a comparison between taking a player now and taking the
best remaining player at your next turn. That needs a probability that a given
player survives that long.

Fitting draft position against consensus ADP across 246 twelve-team PPR drafts
gives a standard deviation of about `0.107 x ADP` -- a player going 40th on
average has a spread of roughly four picks, and one going 120th has about
thirteen. Survival is then the normal tail above your next pick number.

The engine also runs a Monte Carlo of the picks between now and your next turn,
where simulated opponents draft from that ADP distribution weighted by their own
roster needs and any positional run in progress. The decay of that distribution
was calibrated rather than guessed: an initial setting put 56% of simulated pick
mass on players more than twelve picks from their ADP, which disagreed badly
with the analytic curve (one receiver came out 83% gone analytically and 19%
gone in simulation). Sweeping the parameter brought mean disagreement from 0.145
to 0.055.

The two are combined into one number, not added twice. Where the simulation
modeled a player it wins, because it knows about roster needs and runs that the
ADP curve cannot see; the analytic curve covers everyone else.

### Two-turn utility

The final ranking is expected value across your next two picks: the value of
taking this player now, plus the best expected pick at your next turn given that
choice, computed by re-simulating the room for each candidate. Taking the fifth
running back is only good if the alternative at your next turn is meaningfully
worse, and this measures exactly that instead of applying a positional fudge.

### Waivers: opportunity does not beat production

The standard advice is that opportunity is repeatable and production is not, so
you should rank free agents by expected points from usage (xFP) rather than by
points scored. I built that, then replayed the 2025 regular season through it --
weeks 5 to 15, top ten adds each week, graded on the next three weeks:

    season points per game      7.91 PPR/gm    36% returned a startable week
    weighted season + recent    8.04           34%
    expected points (xFP)       7.41           27%
    chase last week's points    6.15           21%
    random available player     3.11            8%

Scoring opportunity instead of production made things **worse**. Stripping out
efficiency throws away real skill along with the touchdown luck; good players
genuinely do more with the same targets.

The second result was stronger. Ranking strategies by how many "emerging"
players they picked lines up almost perfectly against how badly they did, and
the breakout-first strategy loses across the *entire* outcome distribution --
mean, median, 90th percentile and best case alike. Only 4% of its picks returned
a fifteen-point week, against 14% for plain season points.

So the waiver score is season points per game, gated on the role still existing
(a player whose snap share has collapsed is discounted regardless of his
average). Role change and regression risk are reported as context, and neither
moves the number. The expected-points model survives for the one thing it does
well: flagging production that has outrun its usage.

Fitted points per opportunity, from 4,621 player-weeks (R-squared 0.61):

    per target:  RB 1.31   WR 1.71   TE 1.82
    per carry:   0.70 (fitted on running back weeks; other positions are noise)
    quarterback: 0.38 per pass attempt, 1.05 per carry (R-squared 0.43)

Quarterbacks need their own model. Scoring them on targets and carries measures
only their scrambles, which produced nonsense like a starter appearing to score
twenty points above expectation every week.

### Start/sit: the market matters, but less than you would think

The betting market's implied team total is the best public predictor of team
scoring, so it should help pick between two similar players. It does, and the
effect size is worth being honest about.

On genuine toss-ups -- same position, same week, season scoring within 1.5
points, implied totals at least 2 apart -- the player in the better game
environment outscored the other 51.8% of the time, by 0.56 points on average
across 7,656 pairs (t = 4.96). Real, statistically solid, and small: enough to
break a tie, never enough to overturn a projection gap.

Wind is the larger effect. In sustained winds of 15 mph or more, quarterback,
receiver and tight end production fell to 0.72 of each player's own season rate
while running backs rose to 1.17 as game scripts turned run-heavy. It is also
rare, roughly 143 player-weeks a season.

Both adjustments are capped well inside the measured effects. At the lineup
level, replaying 400 random rosters across weeks 5 to 17, they changed 9.7% of
lineups, were better 51.7% of the time when they did, and gained 0.03 points per
week overall -- not statistically significant. They are kept because they are
directionally right in every test and cost nothing, but the real value is the
reasons rather than the arithmetic. "22 mph wind" is worth knowing even in the
weeks the half-point nudge changes nothing.

### Risk and reward

Two meters per player, both fitted rather than invented.

**Risk** is a calibrated probability of a dud week (under half the player's own
scoring rate), fitted on 3,484 point-in-time player-weeks:

    P(bust) = 0.125 + 0.263 x (1 - snap share) + 0.132 x volatility

The R-squared is only 0.11, which is the honest ceiling for predicting a single
NFL week. What matters for a meter is calibration, and that is close to exact:

    predicted   20%   30%   40%   50%   60%
    actual      22%   29%   38%   46%   67%

Signals that did **not** predict busting, and were left out: sample size (flat
at 35% regardless of games played), the expected-points gap, and whether the
player was emerging.

**Reward** is the ceiling, normalized against what the position offers, since
twelve points is an elite tight end week and a flex start for a running back:

    ceiling = 2.86 + 1.09 x scoring rate

Volatility was in that fit too and came out at -0.0155, which is nothing. That
is the third independent time the data said the same thing: **past volatility
does not buy upside.** Within equal-quality bands, volatile players had lower
ceilings, lower floors and lower means than steady ones.

Which means the honest caveat about the pairing: risk and reward correlate at
-0.78. Sorted into quadrants, "high reward, high risk" held one player out of
521 real waiver candidates. Good players are both safer and better. The two
meters answer two real questions, but they are not a tradeoff dial, and the
interface does not pretend you are buying upside with risk.

## Testing

The engine is pure functions over a player pool, so most of it is tested
directly. Beyond unit tests there are two harnesses worth mentioning.

**A golden behaviour lock.** The recommendation pipeline is long and invites
tidying, and tidying is exactly when a scoring change slips through unnoticed --
the rule-based tests keep passing because they assert rules, not numbers. One
test asserts exact scores to three decimals across four draft states, so a
refactor either produces identical output or fails loudly.

**A full-draft rehearsal.** The engine drafts all sixteen rounds against a
simulated room, and the finished roster is graded against invariants a human
would state flatly: never a third quarterback, never a second defense, kickers
and defenses only in the endgame, every starting slot fillable, real depth at
running back and receiver. Every one of those rules is a bug that actually
shipped and was caught in a live mock draft. Now they are caught before a mock.

## Known limitations

- Late-round value is still measured against *starter* replacement levels, which
  structurally flatters backup quarterbacks and tight ends over bench depth at
  running back and receiver. Positional holds contain the damage; a bench-aware
  replacement model is the real fix.
- Waiver and start/sit models were validated on a single season. The logic
  transfers; the exact coefficients should be refit annually.
- The start/sit backtest used each player's own scoring rate as the baseline
  because historical weekly projections are not retrievable after the fact. In
  use the baseline is a real projection, and the marginal value of the game
  environment adjustments on top of that is untested.
- Auto-draft clicks through the platform's own interface, which means it depends
  on page structure that can change without notice.
