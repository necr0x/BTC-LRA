import requests
import argparse
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
PANAMA = timezone(timedelta(hours=-5))

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_FILE = PROJECT_ROOT / "runtime" / "logs" / "BTC_LRA_RESEARCH_LOG.txt"
DEFAULT_START = "2026-09-24 09:00"
DEFAULT_END = "2026-09-25 23:59"


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Исторический дамп BTCUSDT. Если даты не указаны, "
            "они определяются по временным меткам research log."
        )
    )
    parser.add_argument(
        "--start",
        help="Начало периода в формате YYYY-MM-DD HH:MM",
    )
    parser.add_argument(
        "--end",
        help="Конец периода в формате YYYY-MM-DD HH:MM",
    )
    parser.add_argument(
        "--case",
        help="Оставить в автоматическом окне только блоки с указанным именем, например CASE1 или CASE13",
    )
    parser.add_argument(
        "--pre-hours",
        type=float,
        default=6.0,
        help="Сколько часов добавить до первого события при автоматическом окне",
    )
    parser.add_argument(
        "--post-hours",
        type=float,
        default=1.0,
        help="Сколько часов добавить после последнего события при автоматическом окне",
    )
    parser.add_argument(
        "--log",
        default=str(LOG_FILE),
        help="Путь к research log",
    )
    return parser.parse_args()


def log_window(log_path, case_name=None, pre_hours=6.0, post_hours=1.0):
    """Определяет историческое окно по временным меткам лога.

    Запас до события нужен для анализа формирования LR и OI build-up.
    Запас после события нужен для постанализа продолжения, отскока,
    разворота и MFE/MAE.
    """
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    blocks = re.split(r"={20,}", text)
    stamps = []
    pattern = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) Panama UTC-5\]"
    )

    for block in blocks:
        if case_name and case_name.upper() not in block.upper():
            continue
        for match in pattern.finditer(block):
            try:
                stamps.append(
                    datetime.strptime(
                        match.group(1), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=PANAMA)
                )
            except ValueError:
                pass

    if not stamps:
        return None

    start = min(stamps) - timedelta(hours=pre_hours)
    end = max(stamps) + timedelta(hours=post_hours)
    return start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")


ARGS = parse_args()

if ARGS.start and ARGS.end:
    START = ARGS.start
    END = ARGS.end
else:
    detected = log_window(
        ARGS.log,
        case_name=ARGS.case,
        pre_hours=ARGS.pre_hours,
        post_hours=ARGS.post_hours,
    )
    if detected:
        START, END = detected
    else:
        START, END = DEFAULT_START, DEFAULT_END

print(f"Окно выбрано автоматически: {START} -> {END}")
if ARGS.case:
    print(f"Фильтр лога: {ARGS.case}")

def get(path, params):
    r = requests.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def to_ms(s):
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    dt = dt.replace(tzinfo=PANAMA)
    return int(dt.timestamp() * 1000)

def fmt_time(ms):
    return datetime.fromtimestamp(
        ms / 1000, timezone.utc
    ).astimezone(PANAMA).strftime("%Y-%m-%d %H:%M")

START_MS = to_ms(START)
END_MS = to_ms(END)

print("=" * 115)
print("BTC LRA CASE DUMP")
print(f"Panama UTC-5: {START} -> {END}")
print("=" * 115)

# ============================================================
# KLINES
# ============================================================

def load_klines(tf):
    return get("/fapi/v1/klines", {
        "symbol": SYMBOL,
        "interval": tf,
        "startTime": START_MS,
        "endTime": END_MS,
        "limit": 1500,
    })

def print_klines(tf):
    rows = load_klines(tf)

    print()
    print("=" * 45, tf, "=" * 45)

    for k in rows:
        vol = float(k[5])
        buy = float(k[9])
        sell = vol - buy
        delta = buy - sell
        share = abs(delta) / vol if vol else 0

        print(
            f"{fmt_time(int(k[0]))} | "
            f"O {float(k[1]):.1f} "
            f"H {float(k[2]):.1f} "
            f"L {float(k[3]):.1f} "
            f"C {float(k[4]):.1f} | "
            f"VOL {vol:.1f} | "
            f"BUY {buy:.1f} "
            f"SELL {sell:.1f} | "
            f"DELTA {delta:+.1f} | "
            f"d/vol {share:.1%}"
        )

# ============================================================
# OPEN INTEREST — 5m
# ============================================================

def print_oi():
    rows = get("/futures/data/openInterestHist", {
        "symbol": SYMBOL,
        "period": "5m",
        "startTime": START_MS,
        "endTime": END_MS,
        "limit": 500,
    })

    print()
    print("=" * 40, "OPEN INTEREST 5m", "=" * 40)

    prev = None

    for x in rows:
        oi = float(x["sumOpenInterest"])

        if prev is None:
            doi = 0.0
            pct = 0.0
        else:
            doi = oi - prev
            pct = 100 * doi / prev

        print(
            f"{fmt_time(int(x['timestamp']))} | "
            f"OI {oi:,.3f} BTC | "
            f"dOI {doi:+,.3f} BTC | "
            f"dOI% {pct:+.4f}%"
        )

        prev = oi

# ============================================================
# RUN
# ============================================================

print_klines("5m")
print_oi()
print_klines("1m")

print()
print("=" * 115)
print("COPY ALL OUTPUT AND SEND TO CHATGPT")
print("=" * 115)
