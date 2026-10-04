# LP Hedger — paper trading

Would providing concentrated liquidity on **Uniswap v3 ETH/USDC**, hedged with **put options from
[Derive](https://www.derive.xyz)**, actually make money? This app answers that without risking a cent:
it polls a real pool on **Ethereum, Arbitrum or Base**, simulates an LP position of your size and range,
buys the hedge the policy would buy against Derive's live order book, and tracks the P&L of both.

```
┌──────────────────────┐  slot0 / liquidity / fee growth / swap logs  ┌──────────────────────┐
│  LP Hedger (paper)   │ ◀─────────────────────────────────────────── │ Uniswap v3 ETH/USDC  │
│  local UI, no wallet │                                              └──────────────────────┘
│                      │  instruments / tickers / index               ┌──────────────────────┐
│  simulated LP+hedge  │ ◀─────────────────────────────────────────── │ Derive options (public│
└──────────────────────┘                                              └──────────────────────┘
```

Nothing is signed or sent. There is no wallet, no API key, no account.

## What it shows

- **Pool, last 5 days**: swap volume, fees the pool paid out, and your share of in-range liquidity,
  per day and in total.
- **Would this make money?** For your size and range: fees per day the range *would have earned* over
  the lookback (using the pool's own fee-growth accounting, diluted by your own liquidity), time in
  range, fee APR, the live cost of the hedge (ask price + taker fee, rolled before expiry), net carry
  per day, and a 14-day projection net of entry costs. With a plain-language verdict.
- **Paper LP**: value, fees accrued, impermanent loss, entry costs (swap fee, price impact, gas),
  P&L versus holding.
- **Paper hedge**: legs held, premium paid at the *ask* (not mark), fees, spread cost, mark-to-market,
  payouts at expiry.
- **P&L over time**, **pool volume by day**, and the **what-if ETH moves ±40%** scenario chart.

## Quick start

```bash
git clone <this repo> && cd automated_LP
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt
python run.py               # opens http://127.0.0.1:8787
```

1. Pick **chain** (Ethereum / Arbitrum / Base) and **pool** (0.05% or 0.3%) in Settings, size and
   range, and save. Polling starts automatically; the 5-day history backfills within seconds.
2. Read the **Would this make money?** panel. It already prices the planned position.
3. Click **Open paper LP**. On the next tick the hedge policy buys puts (paper) and the equity curve starts.
4. Leave it running. Fees accrue tick by tick from the pool's fee growth; the hedge is marked every tick,
   rolled before expiry and settled at intrinsic value when it expires.

State lives in `./data` (git-ignored): `config.json`, `paper_state.json`, `history_<chain>_<fee>.json`,
`equity.jsonl`, `events.jsonl`. Set `LP_HEDGER_DATA=/some/dir` to relocate it.

## How the numbers are computed

| Quantity | Method |
|---|---|
| Live fee accrual | Every tick reads `feeGrowthGlobal{0,1}X128`, `liquidity` and `slot0`. When both boundary ticks of your range exist on-chain, fees are the exact `feeGrowthInside` delta × your liquidity (Uniswap's own formula). Otherwise the global growth is credited only while the price is inside your range. Both are scaled by `L_pool / (L_pool + L_yours)` because your liquidity would dilute the pool. |
| 5-day history | Tried per public RPC, in order: (a) the same state read at ~6 historical blocks per day (needs an archive node, e.g. `rpc.mevblocker.io`, `mainnet.base.org`); (b) raw `Swap` events in a 8–20 minute window every 4 hours, scaled to the slot (works on `arb1.arbitrum.io`, `mainnet.base.org`). Each swap carries the active liquidity, so fees per unit liquidity are exact inside the window. Nodes that return empty results for pruned ranges are rejected. Live polls then extend the history in hourly buckets. |
| Volume | From fee growth: `fees / fee_rate`; from logs: the sum of swap inputs. |
| Entry / exit costs | Half the size swapped through the same pool: fee tier + price impact against current active liquidity (average execution = half the move) + gas at the current gas price (`gas_open_units` / `gas_close_units` per chain). |
| Hedge fill | Best ask for what the top of book holds, `slippage_pct` worse for the rest; taker fee `rate × index` per contract capped at 12.5% of the premium, plus the flat base fee. Sells use the bid. Expired legs pay `max(K − S, 0)`. |
| Projection | `fees/day` = lookback fees for your range ÷ days covered; `hedge/day` = (premium + fee) ÷ (days to expiry − roll buffer); horizon net = (fees − hedge) × days − entry costs. |

## Strategy knobs (Settings → Hedge policy)

| Setting | Meaning |
|---|---|
| Mode | `protective_put`: puts = coverage × ETH held at range low. `delta_neutral`: puts = coverage × current ETH delta / \|put delta\|. |
| Coverage | Fraction of the exposure to insure (100% = fully capped below the strike). |
| Strike offset | Strike target relative to range low (0% = at the range low; +5% = slightly higher, more protection, more premium). |
| Target days to expiry | Prefers the listed expiry closest to this. |
| Roll when ≤ days left | Sell the old put and buy a fresh one when it gets this close to expiry. |
| Max premium per purchase | Safety cap as % of LP value; the order is scaled down and a warning logged if exceeded. |
| Re-hedge tolerance | Ignore drifts smaller than this % of the target to save fees. |
| Fill slippage | How much worse than the top of book the paper fill assumes for size beyond the displayed quantity. |

## Modules

| Module | Role |
|---|---|
| `lp_hedger/rpc.py` | Tiny JSON-RPC client: batching (chunked for free-tier limits), retries, error classification. |
| `lp_hedger/pool.py` | Read-only pool view: state, tick data, fee-growth-inside, swap logs, gas price, RPC failover. |
| `lp_hedger/history.py` | Volume / fee intervals, archive and log backfills, daily buckets, range statistics. |
| `lp_hedger/paper.py` | Paper LP (sizing, accrual, costs, valuation), paper hedge (fills, fees, settlement), projection. |
| `lp_hedger/strategy.py` | Hedge policy: target size, instrument selection, roll/trim decisions, scenario table. |
| `lp_hedger/derive.py` | Derive REST client (v2 and v3). Only public endpoints are used here; signing stays for a future live mode. |
| `lp_hedger/uniswap_math.py` | Pure v3 math: ticks ↔ prices, liquidity ↔ amounts, exposure at range bounds. |
| `lp_hedger/engine.py` | Background loop: poll → history → jobs → accrue → hedge → projection → equity. |
| `lp_hedger/server.py`, `static/` | FastAPI JSON API and the single-page UI. |

The on-chain execution code (hot wallet, mint/collect/swap, order signing with a session key) from the
earlier version is in git history (`bcb3610`) should you want to turn a profitable paper strategy live.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Covers the v3 math, the hedge policy, interval/volume math, both backfill methods against a fake node,
paper fills/fees/settlement, projection, RPC batching and the HTTP API.

## Caveats

- Public RPCs are rate-limited and differ in what they serve; the app rotates through several per chain.
  A private RPC URL (ideally archive) in Settings makes the history exact.
- Log-sampled history observes 8–20 minutes out of every 4 hours; bursts between samples are missed.
- Fee accrual assumes your liquidity does not change anyone else's behaviour beyond dilution.
- Hedge settlement ignores Derive's settlement fee; fills assume the quote is still there when you cross it.
- Past volume is not future volume. Not financial advice.
