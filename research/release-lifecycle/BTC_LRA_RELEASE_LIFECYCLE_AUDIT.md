# BTC-LRA release lifecycle audit

- Existing 994 transfer observations were read and preserved.
- `btc-lra-001.py` was not read-modified-written and is outside this research layer.
- Candidate inputs use data at or before candidate time; future bars are used only for outcome metrics.
- BATTLE_RESOLUTION_HOLDING now uses the first bar where two consecutive favorable closed-bar observations are available.
- Releases use one stable `release_id` per actual zone departure; transfer records use links.
- MFE/MAE use non-negative directional excursion from candidate price.
- Liquidations were not reconstructed from OI or candles.

Syntax and replay checks were run after generation.
