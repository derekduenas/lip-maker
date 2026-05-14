"""Alpha trading framework — directional edge harvesting on Kalshi.

Sister pillar to LIP rebate-farming. Where LIP earns rebate by being on the
book, Alpha earns by being RIGHT about outcomes more often than the market.

Same architecture pattern as LIP:
  - DISCOVERY → EDGE → SIZING → EXECUTION → LEARNING → BRAIN

Pillars (each plug into base AlphaPillar interface):
  - music    (HDD album sales, Spotify charts, Billboard)
  - weather  (NOAA ensemble forecasts vs market consensus)
  - sports   (Glicko-2 ratings vs market)
  - awards   (GoldDerby aggregated odds vs market)
"""
