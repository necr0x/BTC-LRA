import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
PANAMA = timezone(timedelta(hours=-5))

# Эти значения можно изменить прямо здесь или передать через --start/--end.
DEFAULT_START = "2026-08-18 00:00"
DEFAULT_END = "2026-08-23 23:59"
DEFAULT_OUTPUT = Path(__file__).with_name("BTC_LRA_DUMP.txt")

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Исторический дамп BTCUSDT в файл. "
            "Файл полностью перезаписывается при каждом запуске."
        )
    )
    parser.add_argument("--start", default=DEFAULT_START, help="Начало: YYYY-MM-DD HH:MM")
    parser.add_argument("--end", default=DEFAULT_END, help="Конец: YYYY-MM-DD HH:MM")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Файл результата; по умолчанию BTC_LRA_DUMP.txt",
    )
    return parser.parse_args()


def to_ms(value):
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=PANAMA)
    return int(dt.timestamp() * 1000)


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(PANAMA).strftime(
        "%Y-%m-%d %H:%M"
    )


def get(path, params):
    response = requests.get(BASE + path, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def load_klines(interval, start_ms, end_ms):
    rows = []
    cursor = start_ms
    step = INTERVAL_MS[interval]

    while cursor <= end_ms:
        page = get(
            "/fapi/v1/klines",
            {
                "symbol": SYMBOL,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1500,
            },
        )
        if not page:
            break

        rows.extend(page)
        last_open = int(page[-1][0])
        next_cursor = last_open + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor

        if len(page) < 1500:
            break

    return rows


def load_oi(start_ms, end_ms):
    rows = []
    cursor = start_ms
    step = INTERVAL_MS["5m"]

    while cursor <= end_ms:
        page_end = min(
            cursor + INTERVAL_MS["5m"] * 499,
            end_ms,
        )
        page = get(
            "/futures/data/openInterestHist",
            {
                "symbol": SYMBOL,
                "period": "5m",
                "startTime": cursor,
                "endTime": page_end,
                "limit": 500,
            },
        )
        if not page:
            break

        rows.extend(page)
        last_time = int(page[-1]["timestamp"])
        next_cursor = last_time + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor

        if len(page) < 500:
            break

    return rows


def format_klines(interval, rows):
    lines = ["", f"[{interval}]"]

    for k in rows:
        volume = float(k[5])
        buy = float(k[9])
        sell = volume - buy
        delta = buy - sell
        share = abs(delta) / volume if volume else 0.0

        lines.append(
            f"{fmt_time(int(k[0]))} | "
            f"O {float(k[1]):.1f} "
            f"H {float(k[2]):.1f} "
            f"L {float(k[3]):.1f} "
            f"C {float(k[4]):.1f} | "
            f"VOL {volume:.1f} | "
            f"BUY {buy:.1f} "
            f"SELL {sell:.1f} | "
            f"DELTA {delta:+.1f} | "
            f"d/vol {share:.1%}"
        )

    lines.append(f"ROWS={len(rows)}")
    return lines


def format_oi(rows):
    lines = ["", "[OPEN INTEREST 5m]"]

    previous = None
    for item in rows:
        oi = float(item["sumOpenInterest"])
        if previous is None:
            doi = 0.0
            pct = 0.0
        else:
            doi = oi - previous
            pct = 100.0 * doi / previous if previous else 0.0

        lines.append(
            f"{fmt_time(int(item['timestamp']))} | "
            f"OI {oi:,.3f} BTC | "
            f"dOI {doi:+,.3f} BTC | "
            f"dOI% {pct:+.4f}%"
        )
        previous = oi

    lines.append(f"ROWS={len(rows)}")
    return lines


def main():
    args = parse_args()
    start_ms = to_ms(args.start)
    end_ms = to_ms(args.end)
    if end_ms < start_ms:
        raise SystemExit("Ошибка: --end раньше --start")

    output = Path(args.output).expanduser()
    sections = [
        "BTC LRA WINDOW DUMP",
        f"SYMBOL={SYMBOL}",
        f"TIMEZONE=Panama UTC-5",
        f"START={args.start}",
        f"END={args.end}",
        "NOTE=Файл полностью перезаписывается при каждом запуске.",
    ]

    for interval in ("5m", "1m"):
        rows = load_klines(interval, start_ms, end_ms)
        sections.extend(format_klines(interval, rows))

    try:
        oi_rows = load_oi(start_ms, end_ms)
        sections.extend(format_oi(oi_rows))
    except requests.HTTPError as exc:
        sections.extend(
            [
                "",
                "[OPEN INTEREST 5m]",
                f"OI_ERROR={exc}",
                "OI не доступен для этого исторического окна через Binance API.",
            ]
        )

    sections.extend(["", "END OF DUMP"])
    output.write_text("\n".join(sections) + "\n", encoding="utf-8")
    print(f"Готово: {output.resolve()}")
    print(f"Окно: {args.start} -> {args.end}")


if __name__ == "__main__":
    main()
