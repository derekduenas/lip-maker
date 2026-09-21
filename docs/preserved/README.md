# Preserved work (not applied)

## `stash0-KXGOVNENOMD-blocklist.patch`

**Source:** `stash@{0}`, created 2026-09-21 when this continuation checked
out `claude/fix-lip-adverse-selection-gl6nt`. The working tree at the time
was on `claude/lip-fixes-saturday` (`c471468`, "Fix LIP clean-exit storm +
add restart flap watcher") with this change uncommitted.

**What it is:** a one-line addition to `SERIES_BLOCKLIST` in
`config/settings.py`, blocking series `KXGOVNENOMD`. Its own comment dates
the decision to 2026-05-14 and gives the reason: a 48-hour post-mortem found
it to be "the next KXTRUEV-shape leak" — 2 settled markets, one strike
(LMAR) costing $172 alone, total true net −$235 against $0.01 of rebate. The
automatic series auto-prune escalator did not catch it, so the block was
manual.

**Status: PRESERVED, NOT APPLIED.** It is a risk control belonging to a
different branch's work. The stash itself is left in place and untouched;
this patch is a durable second copy so the change cannot be lost if the
stash is ever cleared.

**To apply it later** (on the branch it came from, not here):

```bash
git checkout claude/lip-fixes-saturday
git apply docs/preserved/stash0-KXGOVNENOMD-blocklist.patch
```
