"""Domain brains — one per Kalshi market family.

Each brain implements DomainBrain (see base.py):
    can_evaluate(ticker)  — does this brain claim this market?
    predict(market)       — return Prediction (p_yes, confidence, features)

Brains are stateless w.r.t. each call (load model + features fresh each
predict). Persistent state (training weights, calibration scalers) lives
in data/argus/{brain_id}_model_v{n}.json.
"""
