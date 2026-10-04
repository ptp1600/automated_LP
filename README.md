# LP Hedger

Simple local automation that provides concentrated liquidity on **Uniswap v3 ETH/USDC** and
hedges the downside with **put options bought on [Derive](https://www.derive.xyz)** (on-chain,
self-custodial options). Runs on your machine with a local encrypted hot wallet and a one-page UI.

```
┌──────────────┐   mints / collects / rebalances   ┌──────────────────────┐
│  LP Hedger   │ ────────────────────────────────▶ │ Uniswap v3 ETH/USDC  │  (Arbitrum or Base)
│  (local UI)  │                                   └──────────────────────┘
│              │   buys / rolls / sells puts       ┌──────────────────────┐
│  hot wallet  │ ────────────────────────────────▶ │ Derive options       │  (signed orders via API)
└──────────────┘                                   └──────────────────────┘
```

## Why hedge an LP position with puts?

A concentrated LP range `[P_low, P_high]` behaves like a short put: as ETH drops toward `P_low`
the position converts into ETH, and below `P_low` it is 100% ETH with linear downside.
LP Hedger buys ETH puts struck near `P_low`, sized to the ETH the range would hold at `P_low`,
so losses are capped below the range while LP fees pay for the premium. The dashboard shows the
resulting P&L curve for ±40% moves before you commit anything.

## Features

- **One-click LP**: pick a USD size and a range (e.g. ±10%), the app wraps ETH, approves, mints.
- **Automatic hedge**: protective-put (cover ETH-at-range-low) or delta-neutral sizing; chooses
  expiry/strike, buys IOC on Derive, rolls before expiry, trims when over-hedged, enforces a
  premium budget.
- **Fee collection** and optional **auto re-centering** when price leaves the range.
- **Dry run by default**: everything is simulated and logged until you flip the switch.
- **Local hot wallet**: encrypted keystore (web3 secret-storage v3) in `./data`, unlocked with a
  password; the key never leaves the Python process.
- **Zero build step**: Python backend, vanilla JS front-end, binds to `127.0.0.1` only.

## Quick start

```bash
git clone <this repo> && cd automated_LP
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt
python run.py               # opens http://127.0.0.1:8787
```

Then in the UI:

1. **Create the hot wallet** (or import a key). Copy its address.
2. **Fund it** on Arbitrum or Base: a little ETH for gas plus the ETH + USDC you want to deploy.
   For a symmetric range you need roughly half the value in each asset.
3. **Connect Derive**
   - Go to [derive.xyz](https://www.derive.xyz), sign in and deposit USDC collateral.
   - Open **Developers**, copy the **Derive wallet** address and the **subaccount ID** into Settings.
   - Under **Session keys**, register the hot wallet address so it may sign orders.
   - Click **Verify connection**.
4. Review the **Settings**, click **Save**, press **Start**.
5. Click **Open LP position**. The hedge is evaluated on every tick (default 60 s).
6. When you are comfortable with the dry-run log, untick **Dry run**, save, and restart the engine.

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

## How it works

| Module | Role |
|---|---|
| `lp_hedger/uniswap_math.py` | Pure v3 math: ticks ↔ prices, liquidity ↔ amounts, exposure at range bounds, mint planning. |
| `lp_hedger/uniswap.py` | web3 client: pool lookup via factory, read position & pending fees, mint / remove / collect / swap. |
| `lp_hedger/derive.py` | Derive v2 REST client with EIP-712 action signing (verified against the official `derive_action_signing` package). |
| `lp_hedger/strategy.py` | Hedge policy: target size, instrument selection, roll/trim decisions, scenario table. |
| `lp_hedger/engine.py` | Background loop: refresh → run queued jobs → hedge step; dry-run aware; event log. |
| `lp_hedger/server.py` | FastAPI app serving the UI and a small JSON API. |
| `lp_hedger/static/` | The single-page UI. |

State lives in `./data` (git-ignored): `config.json`, `hotwallet.keystore.json`, `state.json`,
`events.jsonl`. Set `LP_HEDGER_DATA=/some/dir` to relocate it.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite covers the Uniswap math, the hedge policy, order signing (cross-checked byte-for-byte
against Derive's official signing package), config handling and the HTTP API.

## Security notes

- Fund the hot wallet only with what you intend to deploy. Treat it like cash in a browser wallet.
- The server binds to localhost. Do not expose it (`--host 0.0.0.0`) on a shared network.
- Session keys on Derive can be scoped and given an expiry; prefer that over using the owner key.
- Protocol constants (domain separator, trade module) are in `lp_hedger/derive.py`; check them
  against Derive's "Protocol Constants" page if Derive upgrades its matching contracts.

## Limitations / roadmap

- ETH/USDC only out of the box. Other majors (e.g. BTC) need a pool preset and `derive.currency`.
- Rebalancing swaps through the same pool; large positions may want a smarter router.
- Hedge P&L in the scenario view uses intrinsic value at expiry, not a live option model.
- Not financial advice. Options can expire worthless; LP positions can underperform holding.
