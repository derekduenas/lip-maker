#!/bin/bash
# Nightly offline fill-toxicity pipeline (PAPER ONLY; never touches the engine).
# harvest live fills -> [optional replay fills] -> incremental dataset build ->
# readiness.json -> bake-off (labelled UNDERPOWERED until READY).
set -uo pipefail
cd "${LIP_ML_SRC:-/opt/lip-ml-src}"
OUT=${LIP_ML_OUT:-/var/lib/lip-maker/ml}
MLPY=${LIP_ML_PY:-/opt/lip-ml-venv/bin/python}
ENGPY=${LIP_ENGINE_PY:-/opt/lip-maker/.venv/bin/python}
MODELS=${LIP_ML_MODELS:-prior,rule_fastmove,logreg,lgbm,catboost}
mkdir -p "$OUT"
echo "[$(date -u +%FT%TZ)] harvest"; $MLPY -m mm.ml.harvest --out-dir "$OUT" || echo "harvest failed (continuing)"
FULL=""
if [ "${LIP_ML_REPLAY:-0}" = "1" ]; then
  # RunLoop replay needs the engine's packages: engine venv, read-only use.
  echo "[$(date -u +%FT%TZ)] replay fills"
  if $ENGPY -m mm.ml.replayfills --out-dir "$OUT" > "$OUT/replay.log" 2>&1; then FULL="--full"; else echo "replay failed"; fi
fi
echo "[$(date -u +%FT%TZ)] build $FULL"; $MLPY -m mm.ml.dataset build --out-dir "$OUT" $FULL || exit 1
echo "[$(date -u +%FT%TZ)] readiness"; $MLPY -m mm.ml.dataset report --out-dir "$OUT" --json "$OUT/readiness.json" || exit 1
echo "[$(date -u +%FT%TZ)] bakeoff"; $MLPY -m mm.ml.bakeoff --out-dir "$OUT" --json "$OUT/bakeoff.json" --models "$MODELS" || echo "bakeoff failed"
echo "[$(date -u +%FT%TZ)] done; $(du -sh "$OUT" | cut -f1) in $OUT"
