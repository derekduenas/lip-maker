"""Read-only capture and offline research CLI. Never submits orders."""
import argparse
import json
from pathlib import Path
from research.profit_ledger import ProfitLedger
from research.maker_replay import ReplayConfig, compare, compare_challenger
from research.market_evidence import markouts, summarize_markouts, evaluate_candidates


def read(path):
    return json.loads(Path(path).read_text())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    ingest = sub.add_parser('ingest')
    ingest.add_argument('--db', required=True)
    ingest.add_argument('--events', required=True, help='JSON array of normalized receipts')
    report = sub.add_parser('report')
    report.add_argument('--db', required=True)
    report.add_argument('--mode', choices=['paper', 'live'], required=True)
    report.add_argument('--asof-ms', type=int, required=True)
    report.add_argument('--books')
    replay = sub.add_parser('replay')
    replay.add_argument('--events', required=True)
    replay.add_argument('--include-challenger', action='store_true')
    replay.add_argument('--config', required=True, help='Explicit fee, latency and queue scenario JSON')
    marks = sub.add_parser('markouts')
    marks.add_argument('--fills', required=True)
    marks.add_argument('--books', required=True)
    alloc = sub.add_parser('evaluate')
    alloc.add_argument('--candidates', required=True)
    alloc.add_argument('--budget-usd', required=True)
    alloc.add_argument('--event-cap-usd', required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('--output', required=True)
    capture.add_argument('--market', required=True)
    capture.add_argument('--seconds', type=float, default=60)
    export = sub.add_parser('export-capture')
    export.add_argument('--capture', required=True)
    export.add_argument('--episode-id', required=True)
    rewards = sub.add_parser('reconcile-rewards')
    rewards.add_argument('--db', required=True)
    rewards.add_argument('--statement', required=True)
    rewards.add_argument('--mapping', required=True)
    rewards.add_argument('--account-id', required=True)
    rewards.add_argument('--expected-total-usd', required=True)
    attack = sub.add_parser('attack')
    attack.add_argument('--episodes', required=True)
    attack.add_argument('--scenarios', required=True)
    attack.add_argument('--cutoff-ms', type=int, required=True)
    reward_rank = sub.add_parser('rank-rewards')
    reward_rank.add_argument('--input', required=True, help='Program, competitor book, candidate economics and timing JSON')
    args = p.parse_args()
    if args.command == 'rank-rewards':
        from research.reward_optimizer import rank_quotes
        result = rank_quotes(**read(args.input))
    elif args.command == 'capture':
        import asyncio
        from research.venue_capture import capture
        result = asyncio.run(capture(args.output,args.market,args.seconds))
    elif args.command == 'export-capture':
        from research.venue_capture import export_capture
        result = export_capture(args.capture,args.episode_id)
    elif args.command == 'reconcile-rewards':
        from research.reward_reconciliation import reconcile_rewards
        result = reconcile_rewards(args.db,args.statement,read(args.mapping),args.account_id,args.expected_total_usd)
    elif args.command == 'attack':
        from research.profitability import attack_profitability
        result = attack_profitability(read(args.episodes),read(args.scenarios),args.cutoff_ms)
    elif args.command == 'ingest':
        ledger = ProfitLedger(args.db)
        events = read(args.events)
        inserted = sum(ledger.append(e) for e in events)
        result = dict(inserted=inserted, duplicates=len(events)-inserted)
    elif args.command == 'report':
        result = ProfitLedger(args.db).report(args.mode, args.asof_ms, read(args.books) if args.books else None)
    elif args.command == 'replay':
        cfg = read(args.config)
        required = {'maker_fee_per_contract_usd', 'exit_fee_per_contract_usd', 'latency_ms', 'queue_multiplier'}
        if not required <= cfg.keys():
            p.error('config must explicitly specify fees, latency_ms and queue_multiplier')
        run = compare_challenger if args.include_challenger else compare
        result = run(read(args.events), ReplayConfig(**cfg))
    elif args.command == 'markouts':
        labels = markouts(read(args.fills), read(args.books))
        result = dict(labels=labels, summary=summarize_markouts(labels))
    else:
        result = evaluate_candidates(read(args.candidates), args.budget_usd, args.event_cap_usd)
    print(json.dumps(result, indent=2, allow_nan=False))
    if isinstance(result, dict) and result.get("status") == "BLOCKED":
        raise SystemExit(2)


if __name__ == '__main__':
    main()
