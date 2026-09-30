# BTC-LRA battle-resolution MFE/MAE audit

The transfer script now uses directional distances from candidate price:
- BUY: MFE = max future high - candidate price; MAE = candidate price - min future low.
- SELL: MFE = candidate price - min future low; MAE = max future high - candidate price.

The previous SELL MAE bug (absolute max future high) was corrected. Existing transfer observations were regenerated; no transfer was removed or filtered.

Transfer observations checked: 994
Formula violations found: 0
Signed directional excursions below zero (not formula violations): 0

MFE/MAE are outcome-only fields and are not used by transfer or battle candidate logic.
