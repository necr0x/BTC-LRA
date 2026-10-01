# ============================================================
# LRA_CASE2.py
#
# BTCUSDT — LOCKED-IN RANGE RESEARCH MONITOR
#
# RESEARCH PURPOSE
# ----------------
# 1. Find a REAL sideways Locked-in Range candidate.
# 2. Reject directional/trending movement.
# 3. Split LR into:
#       LOWER 30%
#       MIDDLE 40%
#       UPPER 30%
# 4. Observe LIVE:
#       - taker BUY / SELL
#       - delta
#       - volume
#       - Open Interest
#       - price response
# 5. Look for observable asymmetry:
#
#       LOWER:
#       aggressive selling + OI growth
#
#       UPPER:
#       aggressive buying + OI growth
#
# 6. Freeze the PRE-BREAKOUT state.
# 7. Observe breakout / continuation / reclaim.
#
# IMPORTANT
# ---------
# This script DOES NOT claim to know exact long/short counts.
# OI alone cannot tell which side opened.
#
# BUY_SIDE / SELL_SIDE means observable asymmetry of:
#       taker aggression + OI behaviour + location inside LR
#
# This is a RESEARCH monitor.
# It does NOT issue LONG/SHORT trade entries.
#
# Binance USD-M Futures public API.
# No API key required.
# ============================================================


import argparse
import csv
import importlib.util
import json
import math
import requests
import time
import statistics
import os
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path



# ============================================================
# BASIC CONFIG
# ============================================================

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

POLL_SECONDS = 300

BASE_URL = "https://fapi.binance.com"
REQUEST_TIMEOUT = 10

PANAMA_TZ = timezone(timedelta(hours=-5))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(
    SCRIPT_DIR,
    "BTC_LRA_RESEARCH_LOG.txt"
)
LOCK_FILE = os.path.join(SCRIPT_DIR, "BTC_LRA_RESEARCH_LOG.lock")


# ============================================================
# LR DETECTION CONFIG
# ============================================================

# 12 x 5m = 60 minutes
RANGE_LOOKBACK = 12

# ATR baseline
ATR_LOOKBACK = 36

# Candidate LR width in percent.
MIN_RANGE_PCT = 0.25
MAX_RANGE_PCT = 1.20

# Reject a "range" dominated by one giant candle.
MAX_SINGLE_BAR_SHARE = 0.55

# LR zones
LOWER_ZONE_SHARE = 0.30
UPPER_ZONE_SHARE = 0.30

# At least this proportion of closes should remain
# within the detected envelope.
MIN_INSIDE_RATIO = 0.75


# ============================================================
# REAL FLAT VALIDATION
# ============================================================

# Price must rotate through the midpoint several times.
MIN_MID_CROSSES = 3

# Both external zones must actually be visited.
MIN_LOWER_VISITS = 2
MIN_UPPER_VISITS = 2

# If first close -> last close covers too much of the entire
# range, this is more likely migration/trend than balance.
MAX_NET_DISPLACEMENT_SHARE = 0.45

# Directional efficiency:
#
# abs(last_close - first_close)
# --------------------------------
# sum(abs(close[i] - close[i-1]))
#
# Near 1 = directional
# Near 0 = rotational
MAX_DIRECTIONAL_EFFICIENCY = 0.45

# Adjacent candles should have some overlap.
MIN_AVG_OVERLAP = 0.20


# ============================================================
# OI CONFIG
# ============================================================

# Keep OI samples only in RAM.
OI_MEMORY_SECONDS = 4 * 60 * 60

# Approximate live OI change over 5 minutes.
OI_COMPARE_SECONDS = 5 * 60

# Interesting OI growth during aggressive flow.
MIN_OI_BUILD_PCT = 0.02


# ============================================================
# AGGRESSION CONFIG
# ============================================================

# abs(delta) / volume
MIN_DELTA_SHARE = 0.20

# Current bar volume relative to recent median.
MIN_VOLUME_RATIO = 1.20


# ============================================================
# BREAKOUT CONFIG
# ============================================================

# Close beyond LR edge by this fraction of ATR.
BREAKOUT_ATR = 0.20

# Return inside LR by this fraction of ATR = reclaim.
RECLAIM_ATR = 0.15

# Observe 12 closed 5m candles = 1 hour after breakout.
POST_BREAKOUT_BARS = 12


# ============================================================
# ASYMMETRY CONFIG
# ============================================================

# One side must exceed the other by this factor
# before we call the observable state asymmetric.
ASYMMETRY_FACTOR = 1.50

# Prevent immediate noise after LR detection.
MIN_LIVE_BARS_FOR_BIAS = 2


# ============================================================
# STATE
# ============================================================

session = requests.Session()

oi_samples = deque()

active_range = None

last_closed_bar_time = None


# ============================================================
# TIME / BASIC HELPERS
# ============================================================

def now_panama():
    return datetime.now(PANAMA_TZ)


def fmt_now():
    return now_panama().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def fmt_time_ms(ms):
    dt = datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    )

    return dt.astimezone(
        PANAMA_TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def safe_mean(values):
    if not values:
        return 0.0

    return sum(values) / len(values)


def safe_median(values):
    if not values:
        return 0.0

    return statistics.median(values)


# ============================================================
# SHARED RESEARCH LOG — NO POPUPS / NO SOUND
# ============================================================

def _shared_log_lock(lock_path, timeout=5.0):
    """Cross-process lock for the shared research log (Windows + POSIX)."""
    class _Lock:
        def __enter__(self):
            self.f = open(lock_path, "a+b")
            deadline = time.time() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.f.seek(0)
                        if self.f.tell() == 0:
                            self.f.write(b"\x00")
                            self.f.flush()
                        self.f.seek(0)
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except (OSError, IOError):
                    if time.time() >= deadline:
                        self.f.close()
                        raise TimeoutError("shared log lock timeout")
                    time.sleep(0.02)

        def __exit__(self, exc_type, exc, tb):
            try:
                self.f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            finally:
                self.f.close()

    return _Lock()


def append_log(text):
    block = "=" * 100 + chr(10) + "CASE=CASE2" + chr(10) + text.rstrip() + chr(10) + "=" * 100 + chr(10)
    try:
        with _shared_log_lock(LOCK_FILE):
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(block)
    except Exception as e:
        print("LOG ERROR:", e)


def emit_event(event_type, title, body, sound="info", show_popup=True):
    # UI/audio intentionally disabled; signature preserved.
    text = f"[{fmt_now()} Panama UTC-5] CASE2 | {event_type} | TF={INTERVAL}" + chr(10) + body
    print()
    print("=" * 10)
    print(text)
    print("=" * 10)
    append_log(text)


# ============================================================
# BINANCE DATA
# ============================================================

def get_klines(limit=200):

    url = (
        BASE_URL
        + "/fapi/v1/klines"
    )

    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": limit
    }

    response = session.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    raw = response.json()

    bars = []

    for k in raw:

        volume = float(k[5])

        taker_buy = float(k[9])

        taker_sell = max(
            0.0,
            volume - taker_buy
        )

        delta = (
            taker_buy
            - taker_sell
        )

        bars.append({

            "open_time":
                int(k[0]),

            "close_time":
                int(k[6]),

            "open":
                float(k[1]),

            "high":
                float(k[2]),

            "low":
                float(k[3]),

            "close":
                float(k[4]),

            "volume":
                volume,

            "buy":
                taker_buy,

            "sell":
                taker_sell,

            "delta":
                delta
        })

    return bars


def get_current_oi():

    url = (
        BASE_URL
        + "/fapi/v1/openInterest"
    )

    params = {
        "symbol": SYMBOL
    }

    response = session.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    return float(
        data["openInterest"]
    )


# ============================================================
# OI MEMORY
# ============================================================

def add_oi_sample(
    timestamp,
    oi
):

    oi_samples.append(
        (
            timestamp,
            oi
        )
    )

    cutoff = (
        timestamp
        - OI_MEMORY_SECONDS
    )

    while (
        oi_samples
        and oi_samples[0][0] < cutoff
    ):

        oi_samples.popleft()


def oi_nearest_before(
    target_timestamp
):

    candidate = None

    for timestamp, oi in oi_samples:

        if timestamp <= target_timestamp:
            candidate = oi

        else:
            break

    return candidate


def current_oi_change(
    current_timestamp,
    current_oi,
    seconds=OI_COMPARE_SECONDS
):

    old_oi = oi_nearest_before(
        current_timestamp
        - seconds
    )

    if (
        old_oi is None
        or old_oi == 0
    ):

        return None, None

    doi = (
        current_oi
        - old_oi
    )

    doi_pct = (
        doi
        / old_oi
        * 100.0
    )

    return (
        doi,
        doi_pct
    )


# ============================================================
# ATR / VOLUME
# ============================================================

def true_range(
    bar,
    previous_close
):

    return max(

        bar["high"]
        - bar["low"],

        abs(
            bar["high"]
            - previous_close
        ),

        abs(
            bar["low"]
            - previous_close
        )
    )


def calculate_atr(
    bars,
    lookback=ATR_LOOKBACK
):

    if len(bars) < lookback + 1:
        return None

    sample = bars[
        -(lookback + 1):
    ]

    values = []

    for i in range(
        1,
        len(sample)
    ):

        values.append(
            true_range(
                sample[i],
                sample[i - 1]["close"]
            )
        )

    return safe_mean(
        values
    )


def volume_baseline(
    bars,
    lookback=36
):

    if len(bars) < lookback:
        return None

    volumes = [
        b["volume"]
        for b in bars[-lookback:]
    ]

    return safe_median(
        volumes
    )


# ============================================================
# REAL LR DETECTION
# ============================================================

def detect_range(closed_bars):

    required = max(
        RANGE_LOOKBACK,
        ATR_LOOKBACK + 1
    )

    if len(closed_bars) < required:
        return None

    atr = calculate_atr(
        closed_bars
    )

    if (
        atr is None
        or atr <= 0
    ):
        return None

    sample = closed_bars[
        -RANGE_LOOKBACK:
    ]

    # --------------------------------------------------------
    # ENVELOPE
    # --------------------------------------------------------

    range_high = max(
        b["high"]
        for b in sample
    )

    range_low = min(
        b["low"]
        for b in sample
    )

    width = (
        range_high
        - range_low
    )

    if width <= 0:
        return None

    mid = (
        range_high
        + range_low
    ) / 2.0

    width_pct = (
        width
        / mid
        * 100.0
    )

    if (
        width_pct
        < MIN_RANGE_PCT
    ):
        return None

    if (
        width_pct
        > MAX_RANGE_PCT
    ):
        return None

    # --------------------------------------------------------
    # ONE HUGE CANDLE FILTER
    # --------------------------------------------------------

    max_bar_range = max(

        b["high"]
        - b["low"]

        for b in sample
    )

    max_bar_share = (
        max_bar_range
        / width
    )

    if (
        max_bar_share
        > MAX_SINGLE_BAR_SHARE
    ):
        return None

    # --------------------------------------------------------
    # INSIDE RATIO
    # --------------------------------------------------------

    tolerance = (
        atr * 0.10
    )

    inside_count = 0

    for b in sample:

        if (
            range_low - tolerance
            <= b["close"]
            <= range_high + tolerance
        ):

            inside_count += 1

    inside_ratio = (
        inside_count
        / len(sample)
    )

    if (
        inside_ratio
        < MIN_INSIDE_RATIO
    ):
        return None

    # --------------------------------------------------------
    # LOWER / UPPER ZONES
    # --------------------------------------------------------

    lower_cut = (
        range_low
        + width
        * LOWER_ZONE_SHARE
    )

    upper_cut = (
        range_high
        - width
        * UPPER_ZONE_SHARE
    )

    lower_visits = 0
    upper_visits = 0

    for b in sample:

        close = b["close"]

        if close <= lower_cut:
            lower_visits += 1

        if close >= upper_cut:
            upper_visits += 1

    if (
        lower_visits
        < MIN_LOWER_VISITS
    ):
        return None

    if (
        upper_visits
        < MIN_UPPER_VISITS
    ):
        return None

    # --------------------------------------------------------
    # MIDPOINT ROTATION
    # --------------------------------------------------------

    mid_crosses = 0

    previous_side = None

    # Ignore tiny noise directly around midpoint.
    dead_zone = (
        width * 0.05
    )

    for b in sample:

        close = b["close"]

        if (
            close
            > mid + dead_zone
        ):
            side = 1

        elif (
            close
            < mid - dead_zone
        ):
            side = -1

        else:
            continue

        if (
            previous_side is not None
            and side != previous_side
        ):
            mid_crosses += 1

        previous_side = side

    if (
        mid_crosses
        < MIN_MID_CROSSES
    ):
        return None

    # --------------------------------------------------------
    # NET DISPLACEMENT
    # --------------------------------------------------------

    first_close = (
        sample[0]["close"]
    )

    last_close = (
        sample[-1]["close"]
    )

    net_displacement = abs(
        last_close
        - first_close
    )

    net_displacement_share = (
        net_displacement
        / width
    )

    if (
        net_displacement_share
        > MAX_NET_DISPLACEMENT_SHARE
    ):
        return None

    # --------------------------------------------------------
    # DIRECTIONAL EFFICIENCY
    # --------------------------------------------------------

    total_close_travel = 0.0

    for i in range(
        1,
        len(sample)
    ):

        total_close_travel += abs(

            sample[i]["close"]
            - sample[i - 1]["close"]

        )

    if total_close_travel <= 0:
        return None

    directional_efficiency = (
        net_displacement
        / total_close_travel
    )

    if (
        directional_efficiency
        > MAX_DIRECTIONAL_EFFICIENCY
    ):
        return None

    # --------------------------------------------------------
    # CANDLE OVERLAP
    # --------------------------------------------------------

    overlap_values = []

    for i in range(
        1,
        len(sample)
    ):

        previous_bar = sample[i - 1]
        current_bar = sample[i]

        overlap_high = min(
            previous_bar["high"],
            current_bar["high"]
        )

        overlap_low = max(
            previous_bar["low"],
            current_bar["low"]
        )

        overlap = max(
            0.0,
            overlap_high - overlap_low
        )

        union_high = max(
            previous_bar["high"],
            current_bar["high"]
        )

        union_low = min(
            previous_bar["low"],
            current_bar["low"]
        )

        union = (
            union_high
            - union_low
        )

        if union > 0:

            overlap_ratio = (
                overlap
                / union
            )

        else:

            overlap_ratio = 0.0

        overlap_values.append(
            overlap_ratio
        )

    avg_overlap = safe_mean(
        overlap_values
    )

    if (
        avg_overlap
        < MIN_AVG_OVERLAP
    ):
        return None

    # --------------------------------------------------------
    # REAL LR CANDIDATE
    # --------------------------------------------------------

    return {

        "start_time":
            sample[0]["open_time"],

        "last_time":
            sample[-1]["open_time"],

        "high":
            range_high,

        "low":
            range_low,

        "mid":
            mid,

        "width":
            width,

        "width_pct":
            width_pct,

        "atr":
            atr,

        "inside_ratio":
            inside_ratio,

        "lower_visits":
            lower_visits,

        "upper_visits":
            upper_visits,

        "mid_crosses":
            mid_crosses,

        "net_displacement_share":
            net_displacement_share,

        "directional_efficiency":
            directional_efficiency,

        "avg_overlap":
            avg_overlap
    }


# ============================================================
# CREATE ACTIVE LR
# ============================================================

def create_range_state(
    candidate,
    current_oi
):

    width = candidate["width"]

    lower_cut = (
        candidate["low"]
        + width
        * LOWER_ZONE_SHARE
    )

    upper_cut = (
        candidate["high"]
        - width
        * UPPER_ZONE_SHARE
    )

    return {

        "detected_at":
            time.time(),

        "historical_start":
            candidate["start_time"],

        "historical_end":
            candidate["last_time"],

        "high":
            candidate["high"],

        "low":
            candidate["low"],

        "mid":
            candidate["mid"],

        "width":
            width,

        "width_pct":
            candidate["width_pct"],

        "atr":
            candidate["atr"],

        "lower_cut":
            lower_cut,

        "upper_cut":
            upper_cut,

        "initial_oi":
            current_oi,

        "last_oi":
            current_oi,

        # Number of NEW closed bars observed
        # after LR was detected.
        "live_bars":
            0,

        "bars_seen":
            set(),

        "last_bias":
            "NONE",

        "zones": {

            "LOWER": {
                "bars": 0,
                "volume": 0.0,
                "buy": 0.0,
                "sell": 0.0,
                "delta": 0.0,

                "sell_aggr_bars": 0,
                "buy_aggr_bars": 0,

                "oi_build_sell": 0.0,
                "oi_build_buy": 0.0
            },

            "MIDDLE": {
                "bars": 0,
                "volume": 0.0,
                "buy": 0.0,
                "sell": 0.0,
                "delta": 0.0,

                "sell_aggr_bars": 0,
                "buy_aggr_bars": 0,

                "oi_build_sell": 0.0,
                "oi_build_buy": 0.0
            },

            "UPPER": {
                "bars": 0,
                "volume": 0.0,
                "buy": 0.0,
                "sell": 0.0,
                "delta": 0.0,

                "sell_aggr_bars": 0,
                "buy_aggr_bars": 0,

                "oi_build_sell": 0.0,
                "oi_build_buy": 0.0
            }
        },

        "breakout":
            None,

        "post_breakout_remaining":
            0
    }


# ============================================================
# PRICE ZONE
# ============================================================

def zone_for_price(
    rng,
    price
):

    if price <= rng["lower_cut"]:
        return "LOWER"

    if price >= rng["upper_cut"]:
        return "UPPER"

    return "MIDDLE"


# ============================================================
# PROCESS LIVE BAR INSIDE LR
# ============================================================

def process_range_bar(
    rng,
    bar,
    current_oi,
    doi,
    doi_pct,
    vol_baseline
):

    bar_id = bar["open_time"]

    if bar_id in rng["bars_seen"]:
        return

    rng["bars_seen"].add(
        bar_id
    )

    rng["live_bars"] += 1

    # Use typical price for zone location rather than
    # only close. This is more representative of where
    # the bar traded.
    typical_price = (
        bar["high"]
        + bar["low"]
        + bar["close"]
    ) / 3.0

    zone = zone_for_price(
        rng,
        typical_price
    )

    z = rng["zones"][zone]

    z["bars"] += 1

    z["volume"] += (
        bar["volume"]
    )

    z["buy"] += (
        bar["buy"]
    )

    z["sell"] += (
        bar["sell"]
    )

    z["delta"] += (
        bar["delta"]
    )

    rng["last_oi"] = (
        current_oi
    )

    if bar["volume"] <= 0:
        return

    delta_share = (
        abs(bar["delta"])
        / bar["volume"]
    )

    if (
        vol_baseline is None
        or vol_baseline <= 0
    ):
        volume_ratio = 0.0

    else:

        volume_ratio = (
            bar["volume"]
            / vol_baseline
        )

    # Only classify meaningful aggression.
    if (
        delta_share
        < MIN_DELTA_SHARE
    ):
        return

    if (
        volume_ratio
        < MIN_VOLUME_RATIO
    ):
        return

    # --------------------------------------------------------
    # SELL AGGRESSION
    # --------------------------------------------------------

    if bar["delta"] < 0:

        z["sell_aggr_bars"] += 1

        if (
            doi is not None
            and doi_pct is not None
            and doi_pct >= MIN_OI_BUILD_PCT
        ):

            z["oi_build_sell"] += max(
                doi,
                0.0
            )

    # --------------------------------------------------------
    # BUY AGGRESSION
    # --------------------------------------------------------

    elif bar["delta"] > 0:

        z["buy_aggr_bars"] += 1

        if (
            doi is not None
            and doi_pct is not None
            and doi_pct >= MIN_OI_BUILD_PCT
        ):

            z["oi_build_buy"] += max(
                doi,
                0.0
            )


# ============================================================
# OBSERVABLE ASYMMETRY
# ============================================================

def range_bias(rng):

    lower = (
        rng["zones"]["LOWER"]
    )

    upper = (
        rng["zones"]["UPPER"]
    )

    # Aggressive selling observed in lower LR.
    lower_sell_pressure = max(
        0.0,
        -lower["delta"]
    )

    # Aggressive buying observed in upper LR.
    upper_buy_pressure = max(
        0.0,
        upper["delta"]
    )

    lower_oi = (
        lower["oi_build_sell"]
    )

    upper_oi = (
        upper["oi_build_buy"]
    )

    # Research score only.
    # NOT probability.
    # NOT literal position size.
    lower_score = (
        lower_sell_pressure
        * (
            1.0
            + lower_oi / 100.0
        )
    )

    upper_score = (
        upper_buy_pressure
        * (
            1.0
            + upper_oi / 100.0
        )
    )

    if (
        lower_score <= 0
        and upper_score <= 0
    ):

        return (
            "NONE",
            lower_score,
            upper_score
        )

    if (
        lower_score
        > upper_score
        * ASYMMETRY_FACTOR
    ):

        return (
            "SELL_SIDE",
            lower_score,
            upper_score
        )

    if (
        upper_score
        > lower_score
        * ASYMMETRY_FACTOR
    ):

        return (
            "BUY_SIDE",
            lower_score,
            upper_score
        )

    return (
        "BALANCED",
        lower_score,
        upper_score
    )


# ============================================================
# RANGE REPORT
# ============================================================

def build_range_report(
    rng,
    current_oi
):

    lower = (
        rng["zones"]["LOWER"]
    )

    middle = (
        rng["zones"]["MIDDLE"]
    )

    upper = (
        rng["zones"]["UPPER"]
    )

    (
        bias,
        lower_score,
        upper_score

    ) = range_bias(rng)

    oi_change = (
        current_oi
        - rng["initial_oi"]
    )

    if rng["initial_oi"] != 0:

        oi_change_pct = (
            oi_change
            / rng["initial_oi"]
            * 100.0
        )

    else:

        oi_change_pct = 0.0

    return (

        f"BTCUSDT | TF={INTERVAL}\n\n"

        f"LR: "
        f"{rng['low']:.1f} — "
        f"{rng['high']:.1f}\n"

        f"Ширина: "
        f"{rng['width']:.1f} USD "
        f"({rng['width_pct']:.3f}%)\n\n"

        f"Новых свечей после обнаружения LR: "
        f"{rng['live_bars']}\n\n"

        f"LOWER 30%\n"
        f"Delta: "
        f"{lower['delta']:+.1f} BTC\n"
        f"Volume: "
        f"{lower['volume']:.1f} BTC\n"
        f"SELL aggression bars: "
        f"{lower['sell_aggr_bars']}\n"
        f"OI build при SELL aggression: "
        f"{lower['oi_build_sell']:+.1f} BTC\n\n"

        f"MIDDLE 40%\n"
        f"Delta: "
        f"{middle['delta']:+.1f} BTC\n"
        f"Volume: "
        f"{middle['volume']:.1f} BTC\n\n"

        f"UPPER 30%\n"
        f"Delta: "
        f"{upper['delta']:+.1f} BTC\n"
        f"Volume: "
        f"{upper['volume']:.1f} BTC\n"
        f"BUY aggression bars: "
        f"{upper['buy_aggr_bars']}\n"
        f"OI build при BUY aggression: "
        f"{upper['oi_build_buy']:+.1f} BTC\n\n"

        f"OI после обнаружения LR: "
        f"{oi_change:+.1f} BTC "
        f"({oi_change_pct:+.3f}%)\n\n"

        f"НАБЛЮДАЕМАЯ АСИММЕТРИЯ: "
        f"{bias}\n"

        f"Lower SELL score: "
        f"{lower_score:.1f}\n"

        f"Upper BUY score: "
        f"{upper_score:.1f}\n\n"

        f"Это НЕ точное количество "
        f"лонгов и шортов.\n"

        f"Это наблюдаемая асимметрия "
        f"taker-flow + OI + location."
    )


# ============================================================
# BIAS CHANGE ALERT
# ============================================================

def check_bias_change(
    rng,
    current_oi
):

    if (
        rng["live_bars"]
        < MIN_LIVE_BARS_FOR_BIAS
    ):
        return

    (
        bias,
        lower_score,
        upper_score

    ) = range_bias(rng)

    if bias not in (
        "SELL_SIDE",
        "BUY_SIDE"
    ):
        return

    if (
        bias
        == rng["last_bias"]
    ):
        return

    rng["last_bias"] = bias

    report = build_range_report(
        rng,
        current_oi
    )

    if bias == "SELL_SIDE":

        explanation = (

            "\n\nНАБЛЮДЕНИЕ:\n"

            "В нижней части LR сильнее "
            "наблюдаемая SELL-агрессия "
            "при создании/сохранении OI.\n\n"

            "Это НЕ SHORT сигнал.\n"

            "Если рынок выйдет ВВЕРХ, "
            "будет особенно интересно "
            "проверить, оказались ли "
            "нижние продавцы locked-in."
        )

    else:

        explanation = (

            "\n\nНАБЛЮДЕНИЕ:\n"

            "В верхней части LR сильнее "
            "наблюдаемая BUY-агрессия "
            "при создании/сохранении OI.\n\n"

            "Это НЕ LONG сигнал.\n"

            "Если рынок выйдет ВНИЗ, "
            "будет особенно интересно "
            "проверить, оказались ли "
            "верхние покупатели locked-in."
        )

    emit_event(

        "LR_ASYMMETRY",

        "CASE2 — АСИММЕТРИЯ ВНУТРИ LR",

        report + explanation,

        sound="asymmetry",

        show_popup=True
    )


# ============================================================
# BREAKOUT DETECTION
# ============================================================

def check_breakout(
    rng,
    bar
):

    atr = rng["atr"]

    up_level = (
        rng["high"]
        + atr
        * BREAKOUT_ATR
    )

    down_level = (
        rng["low"]
        - atr
        * BREAKOUT_ATR
    )

    if (
        bar["close"]
        > up_level
    ):
        return "UP"

    if (
        bar["close"]
        < down_level
    ):
        return "DOWN"

    return None


# ============================================================
# START BREAKOUT STUDY
# ============================================================

def activate_breakout(
    rng,
    direction,
    bar,
    current_oi
):

    (
        bias,
        lower_score,
        upper_score

    ) = range_bias(rng)

    rng["breakout"] = {

        "direction":
            direction,

        "time":
            bar["open_time"],

        "price":
            bar["close"],

        "max_price":
            bar["high"],

        "min_price":
            bar["low"],

        "oi":
            current_oi,

        "pre_bias":
            bias,

        "lower_score":
            lower_score,

        "upper_score":
            upper_score,

        "reclaimed":
            False
    }

    rng["post_breakout_remaining"] = (
        POST_BREAKOUT_BARS
    )

    report = build_range_report(
        rng,
        current_oi
    )

    body = (

        f"{report}\n\n"

        f"====================================\n"

        f"ВЫХОД ИЗ LR: {direction}\n"

        f"Цена выхода: "
        f"{bar['close']:.1f}\n"

        f"Время: "
        f"{fmt_time_ms(bar['open_time'])}\n\n"

        f"PRE-BREAKOUT состояние "
        f"ЗАФИКСИРОВАНО.\n"

        f"Теперь наблюдаем фактический "
        f"результат."
    )

    emit_event(

        "LR_BREAKOUT",

        f"CASE2 — ВЫХОД ИЗ LR {direction}",

        body,

        sound="breakout",

        show_popup=True
    )


# ============================================================
# POST-BREAKOUT STUDY
# ============================================================

def process_post_breakout(
    rng,
    bar,
    current_oi
):

    br = rng["breakout"]

    if br is None:
        return False

    br["max_price"] = max(
        br["max_price"],
        bar["high"]
    )

    br["min_price"] = min(
        br["min_price"],
        bar["low"]
    )

    atr = rng["atr"]

    # --------------------------------------------------------
    # RECLAIM
    # --------------------------------------------------------

    if not br["reclaimed"]:

        if (
            br["direction"]
            == "UP"
        ):

            reclaim_level = (
                rng["high"]
                - atr
                * RECLAIM_ATR
            )

            if (
                bar["close"]
                < reclaim_level
            ):

                br["reclaimed"] = True

                body = (

                    f"BTCUSDT | TF={INTERVAL}\n\n"

                    f"LR: "
                    f"{rng['low']:.1f} — "
                    f"{rng['high']:.1f}\n\n"

                    f"Предыдущий выход: UP\n"

                    f"Цена вернулась внутрь LR.\n"

                    f"Close: "
                    f"{bar['close']:.1f}\n\n"

                    f"PRE-BREAKOUT bias: "
                    f"{br['pre_bias']}\n\n"

                    f"ВОЗМОЖНЫЙ FAILED BREAKOUT / "
                    f"RECLAIM.\n"

                    f"Исследовательское событие. "
                    f"НЕ вход."
                )

                emit_event(

                    "LR_RECLAIM",

                    "CASE2 — ВОЗВРАТ В LR",

                    body,

                    sound="reclaim",

                    show_popup=True
                )

        else:

            reclaim_level = (
                rng["low"]
                + atr
                * RECLAIM_ATR
            )

            if (
                bar["close"]
                > reclaim_level
            ):

                br["reclaimed"] = True

                body = (

                    f"BTCUSDT | TF={INTERVAL}\n\n"

                    f"LR: "
                    f"{rng['low']:.1f} — "
                    f"{rng['high']:.1f}\n\n"

                    f"Предыдущий выход: DOWN\n"

                    f"Цена вернулась внутрь LR.\n"

                    f"Close: "
                    f"{bar['close']:.1f}\n\n"

                    f"PRE-BREAKOUT bias: "
                    f"{br['pre_bias']}\n\n"

                    f"ВОЗМОЖНЫЙ FAILED BREAKOUT / "
                    f"RECLAIM.\n"

                    f"Исследовательское событие. "
                    f"НЕ вход."
                )

                emit_event(

                    "LR_RECLAIM",

                    "CASE2 — ВОЗВРАТ В LR",

                    body,

                    sound="reclaim",

                    show_popup=True
                )

    # --------------------------------------------------------
    # COUNT DOWN POST-BREAKOUT WINDOW
    # --------------------------------------------------------

    rng["post_breakout_remaining"] -= 1

    if (
        rng["post_breakout_remaining"]
        > 0
    ):
        return False

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    direction = (
        br["direction"]
    )

    if direction == "UP":

        excursion = (
            br["max_price"]
            - rng["high"]
        )

        excursion_pct = (
            excursion
            / rng["high"]
            * 100.0
        )

    else:

        excursion = (
            rng["low"]
            - br["min_price"]
        )

        excursion_pct = (
            excursion
            / rng["low"]
            * 100.0
        )

    oi_change = (
        current_oi
        - br["oi"]
    )

    if br["oi"] != 0:

        oi_change_pct = (
            oi_change
            / br["oi"]
            * 100.0
        )

    else:

        oi_change_pct = 0.0

    body = (

        f"BTCUSDT | TF={INTERVAL}\n\n"

        f"LR: "
        f"{rng['low']:.1f} — "
        f"{rng['high']:.1f}\n"

        f"Ширина LR: "
        f"{rng['width_pct']:.3f}%\n\n"

        f"PRE-BREAKOUT bias: "
        f"{br['pre_bias']}\n"

        f"Breakout: "
        f"{direction}\n\n"

        f"Максимальное движение "
        f"за границу LR:\n"

        f"{excursion:.1f} USD "
        f"({excursion_pct:.3f}%)\n\n"

        f"Reclaim: "
        f"{'YES' if br['reclaimed'] else 'NO'}\n\n"

        f"OI после breakout: "
        f"{oi_change:+.1f} BTC "
        f"({oi_change_pct:+.3f}%)\n\n"

        f"РЕЗУЛЬТАТ СОХРАНЁН.\n"

        f"Никакая сторона автоматически "
        f"не объявляется trapped."
    )

    emit_event(

        "LR_RESULT",

        "CASE2 — РЕЗУЛЬТАТ LR",

        body,

        sound="info",

        show_popup=True
    )

    return True


# ============================================================
# LR DETECTED MESSAGE
# ============================================================

def announce_new_range(
    candidate
):

    body = (

        f"BTCUSDT | TF={INTERVAL}\n\n"

        f"НАЙДЕН КАНДИДАТ НА РЕАЛЬНЫЙ LR\n\n"

        f"LR: "
        f"{candidate['low']:.1f} — "
        f"{candidate['high']:.1f}\n"

        f"Ширина: "
        f"{candidate['width']:.1f} USD "
        f"({candidate['width_pct']:.3f}%)\n"

        f"ATR: "
        f"{candidate['atr']:.1f}\n\n"

        f"Inside ratio: "
        f"{candidate['inside_ratio'] * 100:.1f}%\n"

        f"Lower visits: "
        f"{candidate['lower_visits']}\n"

        f"Upper visits: "
        f"{candidate['upper_visits']}\n"

        f"Mid crosses: "
        f"{candidate['mid_crosses']}\n"

        f"Net displacement/range: "
        f"{candidate['net_displacement_share']:.2f}\n"

        f"Directional efficiency: "
        f"{candidate['directional_efficiency']:.2f}\n"

        f"Average overlap: "
        f"{candidate['avg_overlap']:.2f}\n\n"

        f"LR прошёл фильтр бокового движения.\n\n"

        f"С ЭТОГО МОМЕНТА CASE2 начинает "
        f"собирать LIVE taker-flow + OI.\n\n"

        f"Историческому участку до обнаружения "
        f"OI задним числом не приписывается."
    )

    emit_event(

        "LR_DETECTED",

        "CASE2 — НАЙДЕН LR",

        body,

        sound="range",

        show_popup=True
    )


# ============================================================
# CONSOLE STATUS
# ============================================================

def print_status(
    rng,
    price,
    current_oi,
    doi_pct
):

    os.system(
        "cls"
        if os.name == "nt"
        else "clear"
    )

    print("=" * 80)

    print(
        "LRA CASE2 — BTCUSDT "
        "LOCKED-IN RANGE RESEARCH"
    )

    print("=" * 80)

    print()

    print(
        "Time Panama:",
        fmt_now()
    )

    print(
        f"Price:       "
        f"{price:.1f}"
    )

    print(
        f"OI:          "
        f"{current_oi:,.3f} BTC"
    )

    if doi_pct is None:

        print(
            "OI ~5m:      "
            "накапливаем историю..."
        )

    else:

        print(
            f"OI ~5m:      "
            f"{doi_pct:+.4f}%"
        )

    print()

    if rng is None:

        print(
            "LR:          "
            "ПОИСК РЕАЛЬНОГО ФЛЭТА"
        )

        print()

        print(
            "Фильтр требует:"
        )

        print(
            "- посещение обеих сторон"
        )

        print(
            "- переходы через середину"
        )

        print(
            "- низкую directional efficiency"
        )

        print(
            "- перекрытие соседних свечей"
        )

        print(
            "- отсутствие сильной "
            "направленной миграции"
        )

        print()

        print(
            "CASE2 ничего не торгует."
        )

        return

    print(
        f"LR:          "
        f"{rng['low']:.1f} — "
        f"{rng['high']:.1f}"
    )

    print(
        f"Width:       "
        f"{rng['width']:.1f} USD "
        f"({rng['width_pct']:.3f}%)"
    )

    print(
        f"LOWER <=     "
        f"{rng['lower_cut']:.1f}"
    )

    print(
        f"UPPER >=     "
        f"{rng['upper_cut']:.1f}"
    )

    print(
        f"Live bars:   "
        f"{rng['live_bars']}"
    )

    print()

    lower = (
        rng["zones"]["LOWER"]
    )

    middle = (
        rng["zones"]["MIDDLE"]
    )

    upper = (
        rng["zones"]["UPPER"]
    )

    print("LOWER 30%")

    print(
        f"  Delta:         "
        f"{lower['delta']:+.1f}"
    )

    print(
        f"  SELL attacks:  "
        f"{lower['sell_aggr_bars']}"
    )

    print(
        f"  SELL OI build: "
        f"{lower['oi_build_sell']:+.1f}"
    )

    print()

    print("MIDDLE 40%")

    print(
        f"  Delta:         "
        f"{middle['delta']:+.1f}"
    )

    print()

    print("UPPER 30%")

    print(
        f"  Delta:         "
        f"{upper['delta']:+.1f}"
    )

    print(
        f"  BUY attacks:   "
        f"{upper['buy_aggr_bars']}"
    )

    print(
        f"  BUY OI build:  "
        f"{upper['oi_build_buy']:+.1f}"
    )

    print()

    (
        bias,
        lower_score,
        upper_score

    ) = range_bias(rng)

    print(
        f"OBSERVED BIAS: "
        f"{bias}"
    )

    print(
        f"SELL score:    "
        f"{lower_score:.1f}"
    )

    print(
        f"BUY score:     "
        f"{upper_score:.1f}"
    )

    print()

    if (
        rng["breakout"]
        is None
    ):

        print(
            "BREAKOUT:     "
            "НЕТ — LR активен"
        )

    else:

        print(
            f"BREAKOUT:     "
            f"{rng['breakout']['direction']}"
        )

        print(
            f"PRE-BIAS:     "
            f"{rng['breakout']['pre_bias']}"
        )

        print(
            f"RECLAIM:      "
            f"{rng['breakout']['reclaimed']}"
        )

    print()

    print(
        "Research only — "
        "LONG/SHORT entry НЕ выдаётся."
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    global active_range
    global last_closed_bar_time

    print(
        "Starting LRA_CASE2.py ..."
    )

    print(
        "Symbol:",
        SYMBOL
    )

    print(
        "TF:",
        INTERVAL
    )

    print(
        "Analysis: every",
        POLL_SECONDS,
        "seconds"
    )

    print(
        "Timezone: Panama UTC-5"
    )

    print(
        "Event log:",
        LOG_FILE
    )

    print()

    while True:

        try:

            current_timestamp = (
                time.time()
            )

            # =================================================
            # CURRENT OI
            # =================================================

            current_oi = (
                get_current_oi()
            )

            add_oi_sample(
                current_timestamp,
                current_oi
            )

            (
                doi,
                doi_pct

            ) = current_oi_change(
                current_timestamp,
                current_oi
            )

            # =================================================
            # KLINES
            # =================================================

            bars = get_klines(
                limit=200
            )

            if len(bars) < 50:

                time.sleep(
                    POLL_SECONDS
                )

                continue

            # Last candle is still OPEN.
            current_bar = (
                bars[-1]
            )

            closed_bars = (
                bars[:-1]
            )

            last_closed = (
                closed_bars[-1]
            )

            vol_base = (
                volume_baseline(
                    closed_bars[:-1],
                    lookback=36
                )
            )

            # =================================================
            # PROCESS EACH CLOSED BAR ONLY ONCE
            # =================================================

            if (
                last_closed_bar_time
                != last_closed["open_time"]
            ):

                last_closed_bar_time = (
                    last_closed["open_time"]
                )

                # ---------------------------------------------
                # ACTIVE POST-BREAKOUT STUDY
                # ---------------------------------------------

                if (
                    active_range is not None
                    and active_range["breakout"]
                    is not None
                ):

                    finished = (
                        process_post_breakout(
                            active_range,
                            last_closed,
                            current_oi
                        )
                    )

                    if finished:

                        active_range = None

                # ---------------------------------------------
                # ACTIVE LR
                # ---------------------------------------------

                elif (
                    active_range
                    is not None
                ):

                    breakout = (
                        check_breakout(
                            active_range,
                            last_closed
                        )
                    )

                    if breakout is not None:

                        activate_breakout(
                            active_range,
                            breakout,
                            last_closed,
                            current_oi
                        )

                    else:

                        process_range_bar(
                            active_range,
                            last_closed,
                            current_oi,
                            doi,
                            doi_pct,
                            vol_base
                        )

                        check_bias_change(
                            active_range,
                            current_oi
                        )

                # ---------------------------------------------
                # SEARCH FOR NEW LR
                # ---------------------------------------------

                if (
                    active_range
                    is None
                ):

                    candidate = (
                        detect_range(
                            closed_bars
                        )
                    )

                    if (
                        candidate
                        is not None
                    ):

                        active_range = (
                            create_range_state(
                                candidate,
                                current_oi
                            )
                        )

                        announce_new_range(
                            candidate
                        )

            # =================================================
            # CONSOLE
            # =================================================

            print_status(
                active_range,
                current_bar["close"],
                current_oi,
                doi_pct
            )

        except KeyboardInterrupt:

            print()
            print(
                "Stopped by user."
            )

            break

        except requests.RequestException as e:

            print()
            print(
                "BINANCE / NETWORK ERROR:",
                e
            )

        except Exception as e:

            print()
            print(
                "ERROR:",
                repr(e)
            )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# HISTORICAL SWING-BALANCE SCANNER
# ============================================================

SWING_ATR_LOOKBACK = 14
SWING_REVERSAL_ATR_FRACTION = 0.25
LEVEL_MATCH_ATR_FRACTION = 0.75
DEPARTURE_ATR_FRACTION = 0.25
DEPARTURE_CLOSES_REQUIRED = 2


def _replay_adapter(csv_path: Path):
    source = Path(__file__).resolve().parent / "btc-lra-002.py"
    spec = importlib.util.spec_from_file_location("btc_lra_002_replay", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load replay infrastructure: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ReplayAdapter(csv_path)


def _parse_panama(value: str) -> int:
    local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=PANAMA_TZ)
    return int(local.timestamp() * 1000)


def _fmt_local(ts: int | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(PANAMA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _report_balance(balance: dict) -> dict:
    result = dict(balance)
    result["historical_start_local"] = _fmt_local(balance.get("historical_start"))
    result["detected_at_local"] = _fmt_local(balance.get("detected_at"))
    departure = balance.get("departure")
    result["end_or_departure_local"] = _fmt_local(departure.get("ts")) if isinstance(departure, dict) else None
    sequence = []
    high_number = low_number = 0
    for swing in balance.get("confirming_swings", []):
        if swing["kind"] == "SWING_HIGH":
            high_number += 1
            label = f"H{high_number}"
        else:
            low_number += 1
            label = f"L{low_number}"
        sequence.append({"label": label, "kind": swing["kind"], "time": _fmt_local(swing["ts"]), "price": swing["price"], "confirmed_at": _fmt_local(swing["confirmed_at"]), "atr": swing["atr"]})
    result["confirming_swing_sequence"] = sequence
    return result


def _aggregate_completed_tf(bars: list[dict], tf: str) -> list[dict]:
    if tf == "1m":
        return list(bars)
    minutes = {"5m": 5, "1h": 60, "4h": 240}[tf]
    duration = minutes * 60_000
    completed: list[dict] = []
    current: dict | None = None
    for bar in bars:
        bucket = (bar["ts"] // duration) * duration
        if current is None or current["bucket_ts"] != bucket:
            if current is not None:
                completed.append(current)
            current = {
                "ts": bucket,
                "bucket_ts": bucket,
                "observable_at_ts": bar["observable_at_ts"],
                "timestamp": bar["timestamp"],
                "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
                "volume_BTC": bar["volume_BTC"], "taker_buy_BTC": bar["taker_buy_BTC"],
                "taker_sell_BTC": bar["taker_sell_BTC"], "delta_BTC": bar["delta_BTC"], "bars": 1,
                "OI_BTC": bar.get("OI_BTC"), "dOI_BTC": bar.get("dOI_BTC"),
                "oi_sample_time_ts": bar.get("oi_sample_time_ts"), "oi_source": bar.get("oi_source"),
            }
        else:
            current["high"] = max(current["high"], bar["high"])
            current["low"] = min(current["low"], bar["low"])
            current["close"] = bar["close"]
            current["observable_at_ts"] = bar["observable_at_ts"]
            current["volume_BTC"] += bar["volume_BTC"]
            current["taker_buy_BTC"] += bar["taker_buy_BTC"]
            current["taker_sell_BTC"] += bar["taker_sell_BTC"]
            current["delta_BTC"] += bar["delta_BTC"]
            current["bars"] += 1
            if bar.get("OI_BTC") is not None:
                current["OI_BTC"] = bar["OI_BTC"]
                current["dOI_BTC"] = bar.get("dOI_BTC")
                current["oi_sample_time_ts"] = bar.get("oi_sample_time_ts")
                current["oi_source"] = bar.get("oi_source")
    if current is not None:
        completed.append(current)
    for bar in completed:
        bar["timestamp"] = _fmt_local(bar["ts"])
    return completed


def _true_range(bar: dict, previous_close: float | None) -> float:
    if previous_close is None:
        return float(bar["high"] - bar["low"])
    return max(float(bar["high"] - bar["low"]), abs(float(bar["high"] - previous_close)), abs(float(bar["low"] - previous_close)))


class HistoricalBalanceScanner:
    """CASE2 active_range architecture with causal swing-based balance detection."""

    def __init__(self, tf: str):
        self.tf = tf
        self.bars: list[dict] = []
        self.swings: list[dict] = []
        self.active_range: dict | None = None
        self.completed_ranges: list[dict] = []
        self.outside_count = 0
        self.last_test_side: str | None = None
        self.duplicate_count = 0

    def atr(self) -> float | None:
        if len(self.bars) < SWING_ATR_LOOKBACK + 1:
            return None
        values = []
        previous = None
        for bar in self.bars[-(SWING_ATR_LOOKBACK + 1):]:
            values.append(_true_range(bar, previous))
            previous = bar["close"]
        return sum(values[-SWING_ATR_LOOKBACK:]) / SWING_ATR_LOOKBACK

    def _confirm_swing(self, bar: dict) -> dict | None:
        if len(self.bars) < 3:
            return None
        before, candidate, confirmation = self.bars[-3], self.bars[-2], self.bars[-1]
        atr = self.atr()
        if atr is None:
            return None
        reversal = atr * SWING_REVERSAL_ATR_FRACTION
        high_reversal = candidate["high"] >= before["high"] and candidate["high"] >= confirmation["high"] and candidate["high"] - confirmation["close"] >= reversal
        low_reversal = candidate["low"] <= before["low"] and candidate["low"] <= confirmation["low"] and confirmation["close"] - candidate["low"] >= reversal
        if not high_reversal and not low_reversal:
            return None
        if high_reversal and low_reversal:
            high_reversal = candidate["high"] - confirmation["close"] >= confirmation["close"] - candidate["low"]
            low_reversal = not high_reversal
        kind = "SWING_HIGH" if high_reversal else "SWING_LOW"
        return {"kind": kind, "ts": candidate["ts"], "price": candidate["high"] if high_reversal else candidate["low"], "confirmed_at": confirmation["ts"], "atr": atr}

    def _append_swing(self, swing: dict) -> bool:
        if self.swings and self.swings[-1]["kind"] == swing["kind"]:
            better = swing["price"] > self.swings[-1]["price"] if swing["kind"] == "SWING_HIGH" else swing["price"] < self.swings[-1]["price"]
            if better:
                self.swings[-1] = swing
            return False
        self.swings.append(swing)
        self.swings = self.swings[-8:]
        return True

    def _balance_candidate(self, swing: dict) -> dict | None:
        if len(self.swings) < 4:
            return None
        sequence = self.swings[-4:]
        kinds = [x["kind"] for x in sequence]
        if kinds not in (["SWING_HIGH", "SWING_LOW", "SWING_HIGH", "SWING_LOW"], ["SWING_LOW", "SWING_HIGH", "SWING_LOW", "SWING_HIGH"]):
            return None
        highs = [x for x in sequence if x["kind"] == "SWING_HIGH"]
        lows = [x for x in sequence if x["kind"] == "SWING_LOW"]
        tolerance = swing["atr"] * LEVEL_MATCH_ATR_FRACTION
        if abs(highs[1]["price"] - highs[0]["price"]) > tolerance or abs(lows[1]["price"] - lows[0]["price"]) > tolerance:
            return None
        signature = tuple((x["kind"], x["ts"]) for x in sequence)
        return {"signature": signature, "upper": (highs[0]["price"] + highs[1]["price"]) / 2, "lower": (lows[0]["price"] + lows[1]["price"]) / 2, "confirming_swings": sequence, "detected_at": swing["confirmed_at"], "atr_at_detection": swing["atr"], "match_tolerance": tolerance}

    def _start_balance(self, candidate: dict) -> None:
        self.active_range = {
            "tf": self.tf, "historical_start": candidate["confirming_swings"][0]["ts"], "detected_at": candidate["detected_at"],
            "upper": candidate["upper"], "lower": candidate["lower"], "confirming_swings": candidate["confirming_swings"],
            "upper_tests": 0, "lower_tests": 0, "rotations": 0, "departure": None, "excursions": [],
            "atr_at_detection": candidate["atr_at_detection"], "match_tolerance": candidate["match_tolerance"],
            "last_test_side": None, "causal_detection_delay_bars": round((candidate["detected_at"] - candidate["confirming_swings"][0]["ts"]) / ({"1m": 1, "5m": 5, "1h": 60, "4h": 240}[self.tf] * 60_000)),
        }

    def _process_active(self, bar: dict) -> None:
        if self.active_range is None:
            return
        atr = self.atr() or self.active_range["atr_at_detection"]
        outside = bar["close"] > self.active_range["upper"] + atr * DEPARTURE_ATR_FRACTION or bar["close"] < self.active_range["lower"] - atr * DEPARTURE_ATR_FRACTION
        self.outside_count = self.outside_count + 1 if outside else 0
        if self.outside_count >= DEPARTURE_CLOSES_REQUIRED:
            direction = "UP" if bar["close"] > self.active_range["upper"] else "DOWN"
            self.active_range["departure"] = {"ts": bar["ts"], "price": bar["close"], "reason": f"{DEPARTURE_CLOSES_REQUIRED} consecutive closes beyond {direction} boundary by {DEPARTURE_ATR_FRACTION:.2f} ATR"}
            self.completed_ranges.append(self.active_range)
            self.active_range = None
            self.swings = []
            self.outside_count = 0

    def process_bar(self, bar: dict, report_start: int) -> None:
        self.bars.append(bar)
        self._process_active(bar)
        swing = self._confirm_swing(bar)
        if swing is None or not self._append_swing(swing):
            return
        if self.active_range is not None:
            tolerance = max(self.active_range["match_tolerance"], swing["atr"] * LEVEL_MATCH_ATR_FRACTION)
            if abs(swing["price"] - self.active_range["upper"]) <= tolerance:
                side = "upper"; self.active_range["upper_tests"] += 1
            elif abs(swing["price"] - self.active_range["lower"]) <= tolerance:
                side = "lower"; self.active_range["lower_tests"] += 1
            elif swing["price"] > self.active_range["upper"] or swing["price"] < self.active_range["lower"]:
                self.active_range["excursions"].append({"ts": swing["ts"], "price": swing["price"], "kind": swing["kind"]})
                side = None
            else:
                side = None
            if side and self.last_test_side and side != self.last_test_side:
                self.active_range["rotations"] += 1
            if side:
                self.last_test_side = side
            return
        candidate = self._balance_candidate(swing)
        if candidate is not None and candidate["detected_at"] >= report_start:
            self._start_balance(candidate)

    def result(self) -> dict:
        active = self.active_range
        balances = self.completed_ranges + ([active] if active else [])
        return {"tf": self.tf, "balances": [_report_balance(balance) for balance in balances], "active_range": active is not None, "duplicate_or_overlapping_balances": self.duplicate_count, "parameters": {"swing_atr_lookback": SWING_ATR_LOOKBACK, "swing_reversal_atr_fraction": SWING_REVERSAL_ATR_FRACTION, "level_match_atr_fraction": LEVEL_MATCH_ATR_FRACTION, "departure_atr_fraction": DEPARTURE_ATR_FRACTION, "departure_closes_required": DEPARTURE_CLOSES_REQUIRED}}


def run_historical(args: argparse.Namespace) -> dict:
    start_ts, end_ts = _parse_panama(args.start), _parse_panama(args.end)
    if end_ts <= start_ts:
        raise ValueError("--end must be later than --start")
    adapter = _replay_adapter(Path(args.csv))
    source_bars = adapter.bars()
    tf_bars = _aggregate_completed_tf(source_bars, args.tf)
    available = [bar["ts"] for bar in tf_bars]
    if not available or start_ts > available[-1] or end_ts < available[0]:
        raise ValueError(f"Requested window is not covered by MASTER: available {_fmt_local(available[0])}–{_fmt_local(available[-1])}")
    scanner = HistoricalBalanceScanner(args.tf)
    processed = 0
    for bar in tf_bars:
        if bar["ts"] > end_ts:
            break
        if bar["ts"] < start_ts:
            continue
        scanner.process_bar(bar, start_ts)
        processed += 1
    result = scanner.result()
    result.update({"mode": "historical", "csv": str(Path(args.csv).resolve()), "start": _fmt_local(start_ts), "end": _fmt_local(end_ts), "processed_bars": processed, "source_bars": len(source_bars), "tf_bars": len(tf_bars)})
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="LRA CASE2 historical fractal balance scanner")
    parser.add_argument("--mode", choices=["live", "historical"], default="live")
    parser.add_argument("--tf", choices=["1m", "5m", "1h", "4h"], default="5m")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--csv", default=str(Path(__file__).resolve().parent / "data" / "master" / "BTC_LRA_MASTER_20260920_NOW_1M.csv"))
    args = parser.parse_args()
    if args.mode == "historical":
        if not args.start or not args.end:
            parser.error("historical mode requires --start and --end in Panama local time")
        run_historical(args)
    else:
        main()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    cli_main()
