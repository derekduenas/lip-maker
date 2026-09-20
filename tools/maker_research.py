"""Offline research CLI. Never imports or calls an order client."""
import argparse
import json
from pathlib import Path
from research.profit_ledger import ProfitLedger
from research.maker_replay import ReplayConfig, compare
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
    replay.add_argument('--config', required=True, help='Explicit fee, latency and queue scenario JSON')
    marks = sub.add_parser('markouts')
    marks.add_argument('--fills', required=True)
    marks.add_argument('--books', required=True)
    alloc = sub.add_parser('evaluate')
    alloc.add_argument('--candidates', required=True)
    alloc.add_argument('--budget-usd', required=True)
    alloc.add_argument('--event-cap-usd', required=True)
    args = p.parse_args()
    if args.command == 'ingest':
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
        result = compare(read(args.events), ReplayConfig(**cfg))
    elif args.command == 'markouts':
        labels = markouts(read(args.fills), read(args.books))
        result = dict(labels=labels, summary=summarize_markouts(labels))
    else:
        result = evaluate_candidates(read(args.candidates), args.budget_usd, args.event_cap_usd)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
