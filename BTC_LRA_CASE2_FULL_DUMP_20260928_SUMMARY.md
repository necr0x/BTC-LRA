# BTC-LRA CASE2 full market dump

## Scope

- Symbol: `BTCUSDT` perpetual futures
- Timezone: Panama UTC-5
- Requested LR: `83002.9` — `83255.0`
- Actual start: `2026-09-28 00:00:00`
- Actual end: `2026-09-28 10:53:00`
- 1m bars: `654`
- 5m aggregates: `131`
- Missing 1m bars: `0`

## Sources and resolution

- OHLCV and taker buy volume: Binance Futures `/fapi/v1/klines`, interval `1m`.
- Taker sell volume: `total volume - taker buy volume` from the same kline record.
- Delta: `taker_buy_volume_BTC - taker_sell_volume_BTC`.
- OI: Binance Futures `/futures/data/openInterestHist`, period `5m`.
- OI is not represented as 1m data: `OI_BTC`, `dOI_BTC`, and `dOI_pct` are populated only on matching 5m sample timestamps; other 1m rows are blank.
- Missing fields: no local tick-level trades or 1m OI source was available; no interpolation was applied.

## Geometric LR events

| Timestamp | Event | Close |
|---|---|---:|
| 2026-09-28 00:14:00 | touch_or_cross_lr_high | 83292.7 |
| 2026-09-28 00:16:00 | first_entry_into_original_lr | 83222.0 |
| 2026-09-28 00:16:00 | touch_or_cross_lr_high | 83222.0 |
| 2026-09-28 00:23:00 | touch_or_cross_lr_low | 83034.6 |
| 2026-09-28 00:25:00 | first_exit_below_lr | 82979.6 |
| 2026-09-28 00:25:00 | touch_or_cross_lr_low | 82979.6 |
| 2026-09-28 00:26:00 | entry_into_original_lr | 83041.8 |
| 2026-09-28 00:26:00 | first_return_to_lr_from_below | 83041.8 |
| 2026-09-28 00:26:00 | touch_or_cross_lr_low | 83041.8 |
| 2026-09-28 00:30:00 | touch_or_cross_lr_low | 83020.2 |
| 2026-09-28 00:31:00 | touch_or_cross_lr_low | 83003.8 |
| 2026-09-28 00:32:00 | exit_below_lr | 82961.1 |
| 2026-09-28 00:32:00 | touch_or_cross_lr_low | 82961.1 |
| 2026-09-28 00:42:00 | entry_into_original_lr | 83025.7 |
| 2026-09-28 00:42:00 | return_to_lr_from_below | 83025.7 |
| 2026-09-28 00:42:00 | touch_or_cross_lr_low | 83025.7 |
| 2026-09-28 00:43:00 | touch_or_cross_lr_low | 83062.4 |
| 2026-09-28 01:13:00 | touch_or_cross_lr_high | 83221.2 |
| 2026-09-28 01:30:00 | touch_or_cross_lr_low | 83040.7 |
| 2026-09-28 02:23:00 | exit_below_lr | 82975.6 |
| 2026-09-28 02:23:00 | touch_or_cross_lr_low | 82975.6 |
| 2026-09-28 02:25:00 | touch_or_cross_lr_low | 82976.2 |
| 2026-09-28 02:35:00 | touch_or_cross_lr_low | 82988.0 |
| 2026-09-28 02:36:00 | touch_or_cross_lr_low | 82993.2 |
| 2026-09-28 02:37:00 | touch_or_cross_lr_low | 82995.7 |
| 2026-09-28 02:54:00 | entry_into_original_lr | 83011.8 |
| 2026-09-28 02:54:00 | return_to_lr_from_below | 83011.8 |
| 2026-09-28 02:54:00 | touch_or_cross_lr_low | 83011.8 |
| 2026-09-28 02:57:00 | exit_below_lr | 82932.6 |
| 2026-09-28 02:57:00 | touch_or_cross_lr_low | 82932.6 |
| 2026-09-28 03:01:00 | entry_into_original_lr | 83051.7 |
| 2026-09-28 03:01:00 | return_to_lr_from_below | 83051.7 |
| 2026-09-28 03:01:00 | touch_or_cross_lr_low | 83051.7 |
| 2026-09-28 03:05:00 | exit_below_lr | 83000.1 |
| 2026-09-28 03:05:00 | touch_or_cross_lr_low | 83000.1 |
| 2026-09-28 03:16:00 | entry_into_original_lr | 83015.6 |
| 2026-09-28 03:16:00 | return_to_lr_from_below | 83015.6 |
| 2026-09-28 03:16:00 | touch_or_cross_lr_low | 83015.6 |
| 2026-09-28 03:17:00 | exit_below_lr | 82958.7 |
| 2026-09-28 03:17:00 | touch_or_cross_lr_low | 82958.7 |
| 2026-09-28 05:52:00 | entry_into_original_lr | 83011.9 |
| 2026-09-28 05:52:00 | return_to_lr_from_below | 83011.9 |
| 2026-09-28 05:52:00 | touch_or_cross_lr_low | 83011.9 |
| 2026-09-28 05:53:00 | exit_below_lr | 82966.6 |
| 2026-09-28 05:53:00 | touch_or_cross_lr_low | 82966.6 |
| 2026-09-28 06:00:00 | touch_or_cross_lr_low | 82988.8 |
| 2026-09-28 06:01:00 | touch_or_cross_lr_low | 82968.2 |
| 2026-09-28 06:03:00 | touch_or_cross_lr_low | 82985.6 |
| 2026-09-28 06:05:00 | touch_or_cross_lr_low | 82989.1 |
| 2026-09-28 06:06:00 | touch_or_cross_lr_low | 82996.8 |
| 2026-09-28 06:07:00 | touch_or_cross_lr_low | 82989.8 |
| 2026-09-28 06:08:00 | touch_or_cross_lr_low | 82993.5 |
| 2026-09-28 06:16:00 | entry_into_original_lr | 83004.7 |
| 2026-09-28 06:16:00 | return_to_lr_from_below | 83004.7 |
| 2026-09-28 06:16:00 | touch_or_cross_lr_low | 83004.7 |
| 2026-09-28 06:17:00 | touch_or_cross_lr_low | 83049.9 |
| 2026-09-28 06:18:00 | exit_below_lr | 82981.7 |
| 2026-09-28 06:18:00 | touch_or_cross_lr_low | 82981.7 |
| 2026-09-28 06:23:00 | entry_into_original_lr | 83027.9 |
| 2026-09-28 06:23:00 | return_to_lr_from_below | 83027.9 |
| 2026-09-28 06:23:00 | touch_or_cross_lr_low | 83027.9 |
| 2026-09-28 06:24:00 | touch_or_cross_lr_low | 83011.8 |
| 2026-09-28 06:25:00 | exit_below_lr | 82961.8 |
| 2026-09-28 06:25:00 | touch_or_cross_lr_low | 82961.8 |
| 2026-09-28 06:30:00 | touch_or_cross_lr_low | 82965.0 |
| 2026-09-28 06:34:00 | entry_into_original_lr | 83020.2 |
| 2026-09-28 06:34:00 | return_to_lr_from_below | 83020.2 |
| 2026-09-28 06:34:00 | touch_or_cross_lr_low | 83020.2 |
| 2026-09-28 06:36:00 | exit_below_lr | 83000.1 |
| 2026-09-28 06:36:00 | touch_or_cross_lr_low | 83000.1 |
| 2026-09-28 06:37:00 | entry_into_original_lr | 83022.0 |
| 2026-09-28 06:37:00 | return_to_lr_from_below | 83022.0 |
| 2026-09-28 06:37:00 | touch_or_cross_lr_low | 83022.0 |
| 2026-09-28 06:47:00 | exit_below_lr | 82989.4 |
| 2026-09-28 06:47:00 | touch_or_cross_lr_low | 82989.4 |
| 2026-09-28 06:56:00 | entry_into_original_lr | 83018.9 |
| 2026-09-28 06:56:00 | return_to_lr_from_below | 83018.9 |
| 2026-09-28 06:56:00 | touch_or_cross_lr_low | 83018.9 |
| 2026-09-28 07:00:00 | touch_or_cross_lr_low | 83033.7 |
| 2026-09-28 07:01:00 | touch_or_cross_lr_low | 83020.7 |
| 2026-09-28 07:02:00 | touch_or_cross_lr_low | 83018.1 |
| 2026-09-28 07:04:00 | touch_or_cross_lr_high | 83223.8 |
| 2026-09-28 07:05:00 | first_exit_above_lr | 83332.7 |
| 2026-09-28 07:05:00 | touch_or_cross_lr_high | 83332.7 |
| 2026-09-28 07:16:00 | touch_or_cross_lr_high | 83290.3 |
| 2026-09-28 07:25:00 | entry_into_original_lr | 83244.0 |
| 2026-09-28 07:25:00 | first_subsequent_return_from_above | 83244.0 |
| 2026-09-28 07:25:00 | touch_or_cross_lr_high | 83244.0 |
| 2026-09-28 07:26:00 | exit_above_lr | 83334.3 |
| 2026-09-28 07:26:00 | touch_or_cross_lr_high | 83334.3 |
| 2026-09-28 07:32:00 | touch_or_cross_lr_high | 83290.7 |
| 2026-09-28 07:40:00 | entry_into_original_lr | 83239.6 |
| 2026-09-28 07:40:00 | return_from_above | 83239.6 |
| 2026-09-28 07:40:00 | touch_or_cross_lr_high | 83239.6 |
| 2026-09-28 07:41:00 | touch_or_cross_lr_high | 83251.0 |
| 2026-09-28 07:43:00 | touch_or_cross_lr_high | 83253.5 |
| 2026-09-28 07:44:00 | exit_above_lr | 83312.1 |
| 2026-09-28 07:44:00 | touch_or_cross_lr_high | 83312.1 |
| 2026-09-28 09:16:00 | touch_or_cross_lr_high | 83315.6 |
| 2026-09-28 09:18:00 | entry_into_original_lr | 83223.1 |
| 2026-09-28 09:18:00 | return_from_above | 83223.1 |
| 2026-09-28 09:18:00 | touch_or_cross_lr_high | 83223.1 |
| 2026-09-28 09:19:00 | exit_above_lr | 83334.9 |
| 2026-09-28 09:19:00 | touch_or_cross_lr_high | 83334.9 |
| 2026-09-28 09:28:00 | entry_into_original_lr | 83232.1 |
| 2026-09-28 09:28:00 | return_from_above | 83232.1 |
| 2026-09-28 09:28:00 | touch_or_cross_lr_high | 83232.1 |
| 2026-09-28 09:30:00 | exit_above_lr | 83269.8 |
| 2026-09-28 09:30:00 | touch_or_cross_lr_high | 83269.8 |
| 2026-09-28 09:31:00 | entry_into_original_lr | 83071.5 |
| 2026-09-28 09:31:00 | return_from_above | 83071.5 |
| 2026-09-28 09:31:00 | touch_or_cross_lr_high | 83071.5 |
| 2026-09-28 09:33:00 | touch_or_cross_lr_high | 83220.8 |
| 2026-09-28 09:34:00 | exit_above_lr | 83257.2 |
| 2026-09-28 09:34:00 | touch_or_cross_lr_high | 83257.2 |
| 2026-09-28 09:35:00 | touch_or_cross_lr_high | 83334.1 |
| 2026-09-28 09:41:00 | entry_into_original_lr | 83200.0 |
| 2026-09-28 09:41:00 | return_from_above | 83200.0 |
| 2026-09-28 09:41:00 | touch_or_cross_lr_high | 83200.0 |
| 2026-09-28 09:42:00 | touch_or_cross_lr_low | 83019.7 |
| 2026-09-28 09:43:00 | exit_below_lr | 82844.3 |
| 2026-09-28 09:43:00 | touch_or_cross_lr_low | 82844.3 |
| 2026-09-28 09:50:00 | touch_or_cross_lr_low | 82997.0 |
| 2026-09-28 10:00:00 | touch_or_cross_lr_low | 82992.0 |
| 2026-09-28 10:01:00 | touch_or_cross_lr_low | 82980.1 |
| 2026-09-28 10:02:00 | entry_into_original_lr | 83010.4 |
| 2026-09-28 10:02:00 | return_to_lr_from_below | 83010.4 |
| 2026-09-28 10:02:00 | touch_or_cross_lr_low | 83010.4 |
| 2026-09-28 10:03:00 | touch_or_cross_lr_low | 83017.2 |
| 2026-09-28 10:04:00 | exit_below_lr | 82960.9 |
| 2026-09-28 10:04:00 | touch_or_cross_lr_low | 82960.9 |
| 2026-09-28 10:05:00 | entry_into_original_lr | 83009.8 |
| 2026-09-28 10:05:00 | return_to_lr_from_below | 83009.8 |
| 2026-09-28 10:05:00 | touch_or_cross_lr_low | 83009.8 |
| 2026-09-28 10:06:00 | touch_or_cross_lr_low | 83014.0 |
| 2026-09-28 10:07:00 | exit_below_lr | 82949.9 |
| 2026-09-28 10:07:00 | touch_or_cross_lr_low | 82949.9 |
| 2026-09-28 10:09:00 | entry_into_original_lr | 83186.0 |
| 2026-09-28 10:09:00 | return_to_lr_from_below | 83186.0 |
| 2026-09-28 10:09:00 | touch_or_cross_lr_low | 83186.0 |
| 2026-09-28 10:11:00 | exit_below_lr | 82920.3 |
| 2026-09-28 10:11:00 | touch_or_cross_lr_low | 82920.3 |
| 2026-09-28 10:12:00 | entry_into_original_lr | 83009.9 |
| 2026-09-28 10:12:00 | return_to_lr_from_below | 83009.9 |
| 2026-09-28 10:12:00 | touch_or_cross_lr_low | 83009.9 |
| 2026-09-28 10:13:00 | exit_below_lr | 82944.0 |
| 2026-09-28 10:13:00 | touch_or_cross_lr_low | 82944.0 |
| 2026-09-28 10:27:00 | entry_into_original_lr | 83015.6 |
| 2026-09-28 10:27:00 | return_to_lr_from_below | 83015.6 |
| 2026-09-28 10:27:00 | touch_or_cross_lr_low | 83015.6 |
| 2026-09-28 10:29:00 | touch_or_cross_lr_low | 83035.2 |
| 2026-09-28 10:30:00 | touch_or_cross_lr_low | 83011.5 |
| 2026-09-28 10:31:00 | exit_below_lr | 82979.6 |
| 2026-09-28 10:31:00 | touch_or_cross_lr_low | 82979.6 |
| 2026-09-28 10:32:00 | entry_into_original_lr | 83075.8 |
| 2026-09-28 10:32:00 | return_to_lr_from_below | 83075.8 |
| 2026-09-28 10:32:00 | touch_or_cross_lr_low | 83075.8 |
| 2026-09-28 10:34:00 | touch_or_cross_lr_low | 83049.9 |
| 2026-09-28 10:38:00 | exit_above_lr | 83277.7 |
| 2026-09-28 10:38:00 | touch_or_cross_lr_high | 83277.7 |
| 2026-09-28 10:39:00 | touch_or_cross_lr_high | 83267.9 |
| 2026-09-28 10:40:00 | entry_into_original_lr | 83200.0 |
| 2026-09-28 10:40:00 | return_from_above | 83200.0 |
| 2026-09-28 10:40:00 | touch_or_cross_lr_high | 83200.0 |
| 2026-09-28 10:41:00 | touch_or_cross_lr_high | 83249.9 |
| 2026-09-28 10:42:00 | touch_or_cross_lr_high | 83207.7 |
| 2026-09-28 10:46:00 | touch_or_cross_lr_high | 83219.8 |
| 2026-09-28 10:47:00 | exit_above_lr | 83258.9 |
| 2026-09-28 10:47:00 | touch_or_cross_lr_high | 83258.9 |
| 2026-09-28 10:48:00 | entry_into_original_lr | 83200.2 |
| 2026-09-28 10:48:00 | return_from_above | 83200.2 |
| 2026-09-28 10:48:00 | touch_or_cross_lr_high | 83200.2 |
| 2026-09-28 10:50:00 | exit_above_lr | 83269.8 |
| 2026-09-28 10:50:00 | touch_or_cross_lr_high | 83269.8 |
| 2026-09-28 10:51:00 | entry_into_original_lr | 83211.3 |
| 2026-09-28 10:51:00 | return_from_above | 83211.3 |
| 2026-09-28 10:51:00 | touch_or_cross_lr_high | 83211.3 |

## Interpretation boundary

This dump intentionally contains raw market measurements and geometric boundary events only. It does not classify trapped participants, market makers, absorption, advantage transfer, or LR depletion.
