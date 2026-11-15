# Washout-recovery bot — btc-race-24h-20261115-0828

Entry for the make.fun competition `btc-race-24h-20261115-0828`
("Race a BTC trading bot live for 24 hours"). The bot is `main.py`
(Python 3.13, standard library only), speaking the
agent-fun/bars-replay/v1 protocol: one JSON line per minute of
cbBTC bars in, one reply with at most one order out.

## Strategy

Written after a post-mortem of a passive capitulation-wait bot that
sat in cash for an entire live race, and after measuring which
signals actually pay on the frozen dev window once the ~26-40 bps
round-trip cost (4 bps fee + 2 bps slippage + pool impact per side)
is charged:

- Shallow dip-buying and momentum have negative median forward
  returns in a falling window. The edge is in **panic flushes that
  have started to turn**: a drop of at least 120 bps in 30 minutes
  (or 150 bps in 2 hours) whose last 3 bars rise at least 12 bps.
  No turn, no trade — the bot never catches a falling knife.
- Entries are full-size (one position at a time). Exits are built
  for the snap-back: bank 60% of the position at +60 bps, let the
  rest run to +300 bps, with a trailing stop (arms at +70 bps,
  fires 85 bps below the peak), a wide −150 bps stop that survives
  aftershocks, a 300-bar time stop, and a 45-bar sit-out after any
  stop-loss so the same leg down is never re-bought.

## Dev-window result

On the competition's frozen dev window (BTC −4.0% over the 24 h,
buy-and-hold fails the dev gate), a fill-exact simulation of the
harness's own fee/slippage/impact model gives:

- Net PnL **+$23.00** on the $1,000 paper start (dev gate: > $0)
- 15 fills, 5 round trips, 5 of 5 profitable, max drawdown ~2%
- Positive on each half of the window traded standalone
  (+$4.16 / +$17.90); parameters sit on a plateau, not a spike
  (flush threshold 100-140 bps and stop 150-180 bps score the same)

## Layout

- `main.py` — the bot: protocol loop plus a pure `RecoveryBot`
  decision class (indicators -> at most one order per bar).
- `.makefun/owner.json` — entry claim file naming the payout wallet.
- `LICENSE` — MIT.
