#!/usr/bin/env python3
"""Washout-recovery bot for the make.fun cbBTC bars-replay race.

Design notes (from studying how these 24h windows actually pay):

- Shallow dip-buying and momentum both bleed in a falling window:
  their median forward return is negative once the ~26-40 bps
  round-trip cost is charged. The one signal with real edge is a
  panic flush that has started to turn: a fast drop of 60+ bps in
  half an hour (or 150+ bps in two hours) whose last few bars are
  rising again. Those snap back hard and for hours.
- So this bot waits for the flush, demands the turn, then buys big.
  It does not catch the knife mid-fall: no turn, no trade.
- Exits are sized to the snap-back: take-profit around +120 bps, a
  stop wide enough to survive the flush's aftershocks, a trailing
  stop once the trade has run, and a long time stop. After a stop
  the bot sits out a long pause (the same leg down is not re-bought);
  after a profit it may re-enter a fresh flush quickly.
- One position at a time. All sizing derives from the batch's own
  account, so the bot keeps no hidden state that could diverge from
  the harness's books.

Protocol: agent-fun/bars-replay/v1, newline-delimited JSON on stdin
and stdout. hello once, one batch per minute, then done. Orders
decided on a batch fill at the next bar's open. Standard library
only, fully deterministic: no clock, no randomness, no environment.
"""

import json
import sys

# --- Tunable parameters (fractions are unitless, *_BPS in basis points) ---

FAST_DROP_BARS = 30     # fast-flush window
FAST_DROP_BPS = 120     # flush: return over FAST_DROP_BARS <= -FAST_DROP_BPS
SLOW_DROP_BARS = 120    # slow-flush window
SLOW_DROP_BPS = 150     # flush: return over SLOW_DROP_BARS <= -SLOW_DROP_BPS
TURN_BARS = 3           # turn confirmation window
TURN_BPS = 12           # turn: return over TURN_BARS >= +TURN_BPS
REGIME_BARS = 240       # washed-out regime window
REGIME_BPS = 0          # if > 0: entries only while return over
                        # REGIME_BARS <= -REGIME_BPS (0 disables the gate)
TP1_BPS = 60            # first take-profit: bank TP1_FRAC of the position
TP1_FRAC = 0.6          # share of the position sold at TP1
TAKE_PROFIT_BPS = 300   # sell the rest when close >= entry * (1 + TP/1e4)
STOP_LOSS_BPS = 150     # sell when close <= entry * (1 - SL/1e4)
TRAIL_ARM_BPS = 70      # trailing stop arms this far above entry
TRAIL_BPS = 85          # ... and fires this far below the peak since entry
MAX_HOLD_BARS = 300     # time stop: recycle capital out of stale trades
SCRATCH_BARS = 0        # if > 0: scratch a trade that has gone nowhere
SCRATCH_BPS = 30        # ... this far under entry after SCRATCH_BARS bars
SIZE_FRACTION = 1.0     # share of cash spent on each entry
COOLDOWN_BARS = 5       # bars to wait after a profitable exit
STOP_PAUSE_BARS = 45    # bars to sit out after a stop-loss exit
MIN_HISTORY = 130       # bars of history required before the first entry
BUY_MAX_SLIPPAGE_BPS = 150
SELL_MAX_SLIPPAGE_BPS = 500

SYMBOL = "CBBTC"


class RecoveryBot:
    """Pure decision logic: feed closes, read account, get orders."""

    def __init__(self):
        self.closes = []            # float closes, oldest first
        self.in_position = False
        self.entry_price = 0.0      # average entry price (quote per asset)
        self.entry_bar = -1         # index into closes when the fill landed
        self.peak_since_entry = 0.0
        self.last_exit_bar = -10 ** 9
        self.pause_until_bar = -1   # no entries before this bar index
        self._exit_is_stop = False  # reason of the exit decided most recently
        self.qty_seen = 0           # position qty at the last sync
        self.partial_done = False   # TP1 partial sale already filled

    # -- state synchronisation with the harness's account ----------------

    def sync_account(self, account, price_scale, qty_scale):
        """Adopt the harness's account as truth; derive entry facts."""
        qty = 0
        cost = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                qty = int(pos.get("qty", "0"))
                cost = int(pos.get("cost_quote_units", "0"))
        bar_index = len(self.closes) - 1
        if qty > 0:
            if not self.in_position:
                self.in_position = True
                self.entry_bar = bar_index
                self.peak_since_entry = self.closes[-1] if self.closes else 0.0
                self.partial_done = False
            elif qty < self.qty_seen:
                # A partial sale filled since the last sync. The harness
                # scales the position's cost with its qty, so the average
                # entry price derived below stays correct afterwards.
                self.partial_done = True
            if cost > 0:
                # cost (1e-6 quote) / qty (1e-8 asset) -> quote per asset
                self.entry_price = (cost / 1e6) / (qty / 10 ** qty_scale)
        else:
            if self.in_position:
                self.in_position = False
                self.last_exit_bar = bar_index
                self.entry_price = 0.0
                self.peak_since_entry = 0.0
                self.partial_done = False
                if self._exit_is_stop:
                    self.pause_until_bar = bar_index + STOP_PAUSE_BARS
                self._exit_is_stop = False
        self.qty_seen = qty

    # -- indicators --------------------------------------------------------

    def _return_over(self, bars):
        if len(self.closes) <= bars:
            return 0.0
        base = self.closes[-1 - bars]
        return self.closes[-1] / base - 1.0 if base > 0 else 0.0

    def _flush_signal(self):
        """True when a panic flush has printed and started to turn."""
        if self._return_over(TURN_BARS) < TURN_BPS / 10000:
            return False
        fast = self._return_over(FAST_DROP_BARS) <= -FAST_DROP_BPS / 10000
        slow = self._return_over(SLOW_DROP_BARS) <= -SLOW_DROP_BPS / 10000
        return fast or slow

    # -- decisions ---------------------------------------------------------

    def decide(self, cash_quote_units):
        """Return a list of order dicts (at most one) for this batch."""
        if not self.closes:
            return []
        close = self.closes[-1]
        bar_index = len(self.closes) - 1
        if self.in_position:
            return self._exit_orders(close, bar_index)
        return self._entry_orders(close, bar_index, cash_quote_units)

    def _exit_orders(self, close, bar_index):
        if close > self.peak_since_entry:
            self.peak_since_entry = close
        entry = self.entry_price
        if entry <= 0:
            return []
        sell = False
        is_stop = False
        if close <= entry * (1 - STOP_LOSS_BPS / 10000):
            sell = True
            is_stop = True
        elif not self.partial_done and close >= entry * (1 + TP1_BPS / 10000):
            # Bank part of the trade; let the rest run for the full move.
            return [{"side": "sell", "frac": TP1_FRAC}]
        elif close >= entry * (1 + TAKE_PROFIT_BPS / 10000):
            sell = True
        elif (
            self.peak_since_entry >= entry * (1 + TRAIL_ARM_BPS / 10000)
            and close <= self.peak_since_entry * (1 - TRAIL_BPS / 10000)
        ):
            sell = True
        elif (
            bar_index - self.entry_bar >= MAX_HOLD_BARS
            and close < entry * (1 + 10 / 10000)
        ):
            sell = True
            is_stop = True
        elif (
            SCRATCH_BARS > 0
            and not self.partial_done
            and bar_index - self.entry_bar >= SCRATCH_BARS
            and close < entry * (1 - SCRATCH_BPS / 10000)
        ):
            # The flush did not snap back: cut it before the full stop.
            sell = True
            is_stop = True
        if not sell:
            return []
        self._exit_is_stop = is_stop
        return [{"side": "sell", "all": True}]

    def _entry_orders(self, close, bar_index, cash_quote_units):
        if len(self.closes) < MIN_HISTORY:
            return []
        if bar_index - self.last_exit_bar < COOLDOWN_BARS:
            return []
        if bar_index < self.pause_until_bar:
            return []
        if REGIME_BPS > 0 and self._return_over(REGIME_BARS) > -REGIME_BPS / 10000:
            return []
        if not self._flush_signal():
            return []
        notional = int(cash_quote_units * SIZE_FRACTION)
        if notional < 1_000_000:  # below the 1 USDC minimum buy
            return []
        return [{"side": "buy", "notional": notional}]


def main():
    bot = RecoveryBot()
    price_scale = 5
    qty_scale = 8
    cash = 0
    position_qty = "0"
    out = sys.stdout

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mtype = msg.get("type")
        if mtype == "hello":
            for sym in msg.get("symbols", []):
                if sym.get("symbol") == SYMBOL:
                    price_scale = int(sym.get("price_scale", 5))
                    qty_scale = int(sym.get("qty_scale", 8))
            cash = int(msg.get("capital", {}).get("quote_units", "0"))
            continue
        if mtype == "done":
            break
        if mtype != "batch":
            continue

        seq = msg.get("seq", 0)
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", cash))
        position_qty = "0"
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                position_qty = pos.get("qty", "0")

        bar = None
        for b in msg.get("bars", []):
            if b.get("symbol") == SYMBOL:
                bar = b
        if bar is not None:
            bot.closes.append(int(bar["c"]) / 10 ** price_scale)
            bot.sync_account(account, price_scale, qty_scale)

        orders = []
        if bar is not None:
            for want in bot.decide(cash):
                if want["side"] == "buy":
                    orders.append(
                        {
                            "side": "buy",
                            "client_id": "b%d" % seq,
                            "symbol": SYMBOL,
                            "notional_quote_units": str(want["notional"]),
                            "max_slippage_bps": BUY_MAX_SLIPPAGE_BPS,
                        }
                    )
                elif want["side"] == "sell" and int(position_qty) > 0:
                    sell_qty = position_qty
                    if "frac" in want:
                        part = int(int(position_qty) * want["frac"])
                        if 0 < part < int(position_qty):
                            sell_qty = str(part)
                    orders.append(
                        {
                            "side": "sell",
                            "client_id": "s%d" % seq,
                            "symbol": SYMBOL,
                            "qty": sell_qty,
                            "max_slippage_bps": SELL_MAX_SLIPPAGE_BPS,
                        }
                    )
        reply = {"type": "reply", "seq": seq, "orders": orders}
        out.write(json.dumps(reply) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
