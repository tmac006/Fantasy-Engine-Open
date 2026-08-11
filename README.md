# Fantasy Football Draft and Season Assistant

## Purpose

Most fantasy football tools either charge a subscription or hand back the same
rankings list that everyone else is already looking at. The question I actually
have when I am on the clock is narrower than that. Given the roster I have built
so far, and given what this particular room is likely to take in the twelve
picks before my next turn, who should I take right now?

So I built something that answers that, using real math and the fact that I like
football enough to spend a lot of evenings on it. Every model here was fitted
against an actual season rather than assumed, and in several cases the data
disagreed with my design badly enough that the design changed. It is still a
work in progress. In testing so far it has held up well.

## What it does

A Chrome side panel watches your live draft on ESPN or Sleeper and recommends a
pick with the reasoning attached. It gives two answers rather than one: the
strongest pick for your roster, and the player most likely to be gone before you
pick again. When those two disagree, the disagreement is usually the useful
part.

Once the season starts, the same engine ranks waiver targets by what a player is
worth per game, and sets a lineup using projections adjusted for the betting
market's read on each game. Genuine coin flips get labelled as coin flips.
There is also a small web app for league status, ingest freshness and news
filtered down to players who could plausibly matter to you.

This is a personal project rather than a product. It reads public data plus your
own leagues, and it is not hosted anywhere.

## How to run

You need Python 3.12 with [uv](https://docs.astral.sh/uv/) and Node 24 or newer.
Docker is optional, since the database is an embedded Postgres that runs in user
space.

    git clone <this repo>
    cd Fantasy-Engine-Open
    cp .env.example .env
    ./scripts/draft-day.sh

That one script installs dependencies, applies migrations, refreshes any stale
market data and starts the API. The web app is then at
`http://localhost:8000/app`.

For the draft assistant, build the extension and load it unpacked:

    cd extension && npm install && npm run build

Open `chrome://extensions`, enable Developer mode, choose Load unpacked and
select the `extension/` directory. Start a draft on either platform and the side
panel picks it up on its own.

Tests:

    uv run pytest
    cd extension && npm test

You can also watch the engine draft an entire team against a simulated room,
which prints every pick with its reasoning and then grades the finished roster:

    uv run python -m api.eval.mockdraft --slot 4 --seed 1

Before a real draft, rehearse against the leagues you actually play in. Roster
settings drive replacement level and how much bench depth is worth, so a league
with a second flex slot produces different advice from one without:

    uv run python -m api.eval.rehearse --seeds 3

ESPN private league reads need your own session cookies in `.env`, but nothing
about drafting requires them, because the extension uses the session you are
already logged into.

## Some of the math

### Value over replacement

A player is worth what he scores above whatever you could get for free at his
position, and that replacement line comes from your league settings rather than
from a generic list. In a twelve team league running two backs plus a flex, it
lands around the twenty ninth running back. Quarterbacks put up more raw points
than running backs, yet the distance between the best quarterback and a freely
available one is small, which is why the engine will happily pass on the higher
scoring player.

### Will he last until my next pick

Since every pick is really a comparison between taking someone now and taking
whoever survives to your next turn, the engine needs a survival probability.
Fitting real draft position against consensus ADP across 246 twelve team PPR
drafts gives a spread of about 0.107 times ADP, so a player going fortieth on
average moves by roughly four picks while one going 120th moves by thirteen.

A Monte Carlo of the intervening picks sharpens that, with simulated opponents
drafting from the ADP distribution weighted by their own roster holes. Getting
the decay right mattered more than I expected. An early setting put 56 percent
of simulated pick mass on players more than twelve picks away from their ADP,
which disagreed badly with the analytic curve; one receiver came out 83 percent
gone by one method and 19 percent gone by the other. Sweeping that parameter
brought the mean disagreement between the two down from 0.145 to 0.055.

### Waivers, where the data changed my mind

Conventional wisdom holds that opportunity repeats while production does not, so
free agents ought to be ranked by expected points from usage rather than by
points actually scored. I built exactly that, then replayed the 2025 regular
season through it, taking the top ten adds each week from week five to week
fifteen and grading them on the following three weeks.

    season points per game        7.91 PPR/gm
    weighted season and recent    8.04
    expected points from usage    7.41
    chase last week's points      6.15
    random available player       3.11

Ranking by usage finished behind ranking by plain scoring average. Pulling
efficiency out of the score throws away real skill alongside the touchdown luck,
because good players genuinely do more with the same number of targets.

The second finding was blunter. Sorting strategies by how many breakout
candidates they selected tracks almost exactly against how badly they did, and a
breakout first approach loses at every point of the outcome distribution rather
than trading a worse average for a better ceiling. Four percent of its picks
returned a fifteen point week, against fourteen percent for plain season
scoring. Role changes still appear on the card as context, since a human may
know something the model does not, but they no longer move the score.

### What a bench player is worth

A sixth running back cannot enter a lineup that starts three, yet value over
replacement prices him as though he could. A room that lets backs slide will
therefore draft you a bench nobody can use, which is exactly what happened in
testing: four backs across rounds six through nine while a starting receiver
slot sat empty. The engine now scales a surplus player by the chance a week
actually calls on him, so the first backup behind three startable slots keeps
53 percent of his value, the second keeps 13 percent and the fourth keeps none.

How often a starter goes missing came out of the 2025 season rather than out of
intuition, and picking the sample turned out to matter more than the arithmetic
did. Ranking by season total selects the players who stayed healthy, which is
the one sample incapable of measuring injury, and it put the rate at roughly
half its true value. Choosing on the first three weeks and grading on the rest
gives 22 percent across 1680 player weeks. That figure barely moves between
positions, so one number covers all four; what separates a backup quarterback
from a fourth running back is how many lineup slots stand in front of him.

### Risk and reward

Each player card carries two meters, both fitted rather than eyeballed. Risk is
a calibrated probability of a dud week, driven by snap share and week to week
volatility. Its R-squared is only 0.11, which is roughly the ceiling for
predicting a single NFL week, but calibration is what a meter needs and that
part lines up closely: buckets predicted at 20, 30, 40 and 50 percent came in at
22, 29, 38 and 46 percent.

Reward is a projected ceiling normalised by position, since twelve points is an
elite week for a tight end and a flex start for a running back. Volatility went
into that fit too and came out at -0.0155, which is nothing. Past boom and bust
does not buy you upside; inside equal quality bands, volatile players had lower
ceilings and lower floors than steady ones.

## Testing

Most of the engine is pure functions over a player pool, so it is tested
directly. Two harnesses beyond the unit tests are worth mentioning.

One is a golden behaviour lock. The recommendation pipeline is long and invites
tidying, and tidying is precisely when a scoring change slips past unnoticed,
because rule based tests keep passing when they assert rules instead of numbers.
So one test pins exact scores to three decimal places across four draft states.
A refactor either reproduces them or fails loudly.

The other is the full draft rehearsal mentioned above. After sixteen simulated
rounds the finished roster gets checked against rules a human would state
flatly: never a third quarterback, never a second defense, kickers and defenses
only at the very end, every starting slot fillable, real depth at running back
and receiver, nobody stacked so deep behind a position that no lineup could
field him. Each of those is a bug that shipped at some point and got caught in
a live mock draft. They are caught earlier now.

The rehearsal scores the finished roster on what its best legal lineup projects
rather than on the sum of all sixteen picks. Grading by the total was itself
hiding a failure, since a sixth running back adds his whole projection to that
number while contributing nothing you can start.

## Known limitations

Replacement level is a property of the league rather than of your roster, so
the first receiver you draft and the third get measured against the same line.
Past a full lineup the depth discount takes over, which is where the damage
used to be, though the picks before that still lean on one baseline per
position.

The waiver and start/sit models were validated against a single season. The
logic should transfer; the coefficients want refitting every year.

The start/sit backtest used each player's own scoring rate as its baseline,
since historical weekly projections cannot be retrieved after the fact. In
practice the baseline is a real projection, and how much the game environment
adjustments add on top of that is untested.

Auto draft works by clicking through the platform's own interface, so it depends
on page structure that can change without warning.
