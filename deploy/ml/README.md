# Offline fill-toxicity pipeline (paper only)

Runs outside the engine from its own checkout, `/opt/lip-ml-src` (same repo,
updated with `git -C /opt/lip-ml-src pull --ff-only`), with the separate
`/opt/lip-ml-venv` (numpy, pandas, scikit-learn, lightgbm, catboost, torch-cpu,
tabpfn 2.0.9). The engine checkout `/opt/lip-maker` and its `.venv` are not
touched; the engine never imports `mm.ml`.

Install (root):

    git clone -b apex/patches-1-17 https://github.com/derekduenas/lip-maker /opt/lip-ml-src
    install -d -o lip -g lip /var/lib/lip-maker/ml
    cp /opt/lip-ml-src/deploy/ml/lip-ml-*.{service,timer} /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now lip-ml-harvest.timer lip-ml-nightly.timer

* `lip-ml-harvest.timer` every 10 min: live paper fills -> `/var/lib/lip-maker/ml/fills_live.jsonl`
* `lip-ml-nightly.timer` 10:30 UTC (03:30 PT): harvest, incremental dataset
  build, `/var/lib/lip-maker/ml/readiness.json`, `bakeoff.json`.
  `LIP_ML_REPLAY=1` (drop-in) adds a RunLoop replay of all closed recordings
  (~1 CPU-hour per day of recordings) and a full rebuild.

Both run as `lip`, nice 19, idle I/O, CPUQuota (100 % / 25 %), MemoryMax
(2 G / 300 M), ProtectSystem=strict with only `/var/lib/lip-maker/ml`
writable. Samples are capped at LIP_ML_MAX_GB (2 GB, oldest pruned).
