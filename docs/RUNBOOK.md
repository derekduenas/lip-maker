# LIP paper maker — runbook

## One command

```bash
python tools/run_session.py --duration 600 --top-n 20
```

Runs the whole loop in one process: live discovery, real orderbooks,
economic quote selection, causally simulated fills, one shared $5,000
ledger, per-program reward accrual, inventory exits, and a report.

Useful flags:

| flag | default | why |
|---|---|---|
| `--duration` | 600 | seconds to run |
| `--top-n` | 20 | markets to quote, ranked by pool **rate** ($/sec) |
| `--poll-sec` | 5 | orderbook poll interval |
| `--trade-poll-sec` | 10 | trade poll interval (drives fills and flow stats) |
| `--reuse-discovery` | off | skip the full scan (~1,000 pages, ~2 min) and use the DB |
| `--report` | auto | where to write the JSON report |

First run only:

```bash
python init_db.py
```

## Safety

Paper only, three independent interlocks:

1. `PAPER_MODE` is on unless `LIVE_ARMED`; the command refuses to start otherwise.
2. `LIP_LIVE_ACK` must equal `I_ACCEPT_LIVE_RISK` or `QuoteManager` forces paper.
3. `require_live_execution_allowed()` refuses live transmission while
   `MAKER_ONLY_ENFORCEMENT_VERIFIED` is False. Both live paths call it.

No order reaches the exchange. Nothing here places, cancels or funds anything live.

## What the data actually is

| piece | source | credential |
|---|---|---|
| incentive programs | `GET /incentive_programs` | none |
| orderbooks | `GET /markets/{t}/orderbook` | none |
| trades | `GET /markets/trades` | none |
| **orderbook WebSocket** | `wss://.../trade-api/ws/v2` | **required — 401 without** |
| order placement | `POST /portfolio/orders` | required, and blocked anyway |

With no key this runs on REST snapshots. That is a real limitation, not a
formality: snapshots have no sequence, so gaps are undetectable, and queue
position and the exact instant of a cross are unobservable. Fill evidence
from a REST run is weaker than from a WS run, and the report says so.

### Exact secure setup for WebSocket capture

Checked 2026-09-21 without printing any secret: `KALSHI_KEY_ID` unset,
`KALSHI_API_KEY` unset, `KALSHI_PRIVATE_KEY_PATH` unset, no `.env`, and no
key file at the configured default. So WS capture is unavailable here and
the loop runs on REST.

To enable it:

1. In the Kalshi web UI create an API key. Keep the **private** key it
   gives you; Kalshi keeps only the public half.
2. Save the private key outside the repo and lock it down:

   ```bash
   mkdir -p ~/.config/kalshi && chmod 700 ~/.config/kalshi
   mv ~/Downloads/kalshi_private_key.pem ~/.config/kalshi/
   chmod 600 ~/.config/kalshi/kalshi_private_key.pem
   ```

3. Export the id and the path (a shell profile or a `.env` that is **not**
   committed — `.env` is already ignored):

   ```bash
   export KALSHI_KEY_ID="<the key id shown in the UI>"
   export KALSHI_PRIVATE_KEY_PATH="$HOME/.config/kalshi/kalshi_private_key.pem"
   ```

4. Confirm without echoing anything secret:

   ```bash
   python -c "from config import settings; import pathlib; \
     print('id set:', bool(settings.KALSHI_KEY_ID), \
           '| key readable:', pathlib.Path(settings.KALSHI_KEY_PATH).exists())"
   ```

Nothing else changes; the loop is WS-native and will use it automatically.
A read-only key is sufficient for capture, and live order placement stays
blocked by the interlock regardless.

## The entry-cutoff experiment

```bash
python tools/run_experiment.py --capture-sec 900
```

Captures one live stream, then replays it through every cutoff arm, each
with its own independent $5,000 account and database. The policies and the
market-selection spec are frozen in `engine/entry_cutoff.py` and
`engine/experiment_spec.py`, and both fingerprints are written into every
result so a reader can tell whether they were edited after the fact.

## Reading the report

`REWARD` is always an **estimate**. Nothing has been paid or reconciled.
Only `reward_credit` events from `research/reward_reconciliation.py` against
real payment records are money.

`INVENTORY` shows `paired` separately from `net`. Paired contracts are
riskless at settlement but are **not spendable cash** until the venue settles
them; they are capital locked up.

`econ_rejects` is the economic layer declining to quote. That is the system
working, not a failure — a quote must beat not quoting.

## Fee provenance

`docs/venue_evidence/kalshi_fees_20260920.json`. Maker-charging and rounding
are verified against Kalshi; the rate constant `0.07` is **not**, so every
fee-dependent number is assumption-dependent and the report prints the
schedule's `verified` flag.
