"""Offline fill-toxicity (adverse selection) data + evaluation pipeline.

PAPER ONLY, OFFLINE ONLY. Nothing in this package is imported by the engine
(mm.unattended) and nothing here places, pulls or sizes quotes. Modules:

* ``mm.ml.dataset``    point-in-time feature/label builder over the recorder's
                       frame files + the paper fill journals; readiness report.
* ``mm.ml.harvest``    copies live paper fills (status fills_detail + lip.log
                       "paper fill" lines) into a durable journal.
* ``mm.ml.replayfills`` optional: replays recordings through RunLoop (paper) and
                       journals the replay fills and resting-quote samples.
* ``mm.ml.bakeoff``    purged/embargoed walk-forward model bake-off (needs the
                       separate /opt/lip-ml-venv: numpy, scikit-learn, ...).
"""
