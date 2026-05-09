# FOMC May 7, 2026 — Game Day Plan

## THE SEQUENCE

### May 1-5: Pre-Event
- [ ] Deposit bankroll to Kalshi ($500-1,000)
- [ ] Verify balance: `ssh root@147.182.138.189 'cd /root/sovereign && python3 tools/pre_live_audit.py'`
- [ ] Review shadow week results: `python3 tools/shadow_monitor.py`
- [ ] Confirm shadow shows no systemic issues

### May 5: Flip Live
```bash
ssh root@147.182.138.189 "
  cd /root/sovereign
  sed -i 's/PAPER_MODE = True/PAPER_MODE = False/' config/settings.py
  sed -i 's/SHADOW_MODE = True/SHADOW_MODE = False/' config/settings.py
  systemctl restart sovereign
"
```
- [ ] Verify: `curl http://147.182.138.189:8080/status` shows mode=LIVE

### May 6, 6:00am ET: Scanner Fires (T-24h)
The morning scan auto-detects FOMC May 7 mention markets.
- Edge report generated for all FOMC terms
- Junk bond candidates identified (95%+ base rate below 90c)
- HV candidates identified (15pp+ edge, 10-60c range)
- First-line trades placed automatically

**Check notification.** Expect 8-15 qualifying trades:
- 5-8 junk bonds (inflation, unemployment, labor market, etc. below 90c)
- 3-5 HV trades (tariffs, disinflation, soft landing, etc.)

**DO NOT INTERVENE** unless:
- Scanner found 0 opportunities → investigate
- Scanner found >15 → check for false positives
- Error in logs

### May 7, 2:00pm ET: FOMC Statement Released
- Statement markets may resolve first
- Monitor: `ssh root@147.182.138.189 'tail -f /root/sovereign/logs/sovereign.log'`

### May 7, 2:30pm ET: Press Conference Starts
- Presser mention markets begin resolving as Powell speaks
- Junk bond positions should start settling YES almost immediately
- HV positions resolve over 45-60 minutes

### May 7, 4:00pm ET: Press Conference Ends
- All presser markets should resolve within 2 hours of transcript
- Evening review at 8pm auto-scores everything

### May 7, 8:00pm ET: Evening Review
- Reviewer fires automatically
- All trades scored, PnL computed
- Lessons extracted to LESSONS.md
- Check phone for summary notification

### May 8, Morning: Retrospective
```bash
ssh root@147.182.138.189 'cd /root/sovereign && python3 -c "
import sqlite3
conn = sqlite3.connect(\"data/sovereign.db\")
print(\"FOMC MAY 7 RESULTS:\")
for r in conn.execute(\"SELECT term, side, price, contracts, outcome, pnl FROM trades WHERE outcome IN (\\\"WIN\\\",\\\"LOSS\\\") ORDER BY placed_at DESC LIMIT 20\").fetchall():
    pnl = f\"+{r[5]:.2f}\" if r[5] >= 0 else f\"{r[5]:.2f}\"
    print(f\"  {r[0]:<25} {r[1]} @{r[2]*100:.0f}c x{r[3]} [{r[4]}] {pnl}\")
conn.close()
"'
```

## EMERGENCY ROLLBACK
If anything goes wrong during live trading:
```bash
ssh root@147.182.138.189 "
  cd /root/sovereign
  sed -i 's/PAPER_MODE = False/PAPER_MODE = True/' config/settings.py
  systemctl restart sovereign
"
```
This immediately stops all live order placement. Open positions remain but no new ones are created.

## WHAT SUCCESS LOOKS LIKE
- 8+ trades placed
- Junk bonds: 95%+ resolution rate
- HV trades: 60%+ resolution rate  
- Blended WR: 75%+
- PnL: positive (any amount — first event is validation, not profit target)

## WHAT TRIGGERS PAUSE
- WR < 50% on 8+ trades → revert to paper, investigate
- Any resolution rule surprise → document in LESSONS.md, adjust rules.py
- Maker orders not filling → adjust entry pricing logic
- API errors during event → check logs, fix before next event
