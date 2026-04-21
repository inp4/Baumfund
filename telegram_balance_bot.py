#!/usr/bin/env python3
import json
import os
import signal
import ssl
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone
from pathlib import Path


ASSETS = [
    {"name": "Bitcoin", "ticker": "BTC", "cg_id": "bitcoin", "qty": 0.01641266},
    {"name": "Ethereum", "ticker": "ETH", "cg_id": "ethereum", "qty": 0.52720906},
    {"name": "Solana", "ticker": "SOL", "cg_id": "solana", "qty": 10.5},
    {"name": "Chainlink", "ticker": "LINK", "cg_id": "chainlink", "qty": 49.37},
    {"name": "Tether Gold", "ticker": "XAUT", "cg_id": "tether-gold", "qty": 0.084256},
    {"name": "Stablecoins", "ticker": "USDT", "cg_id": None, "qty": 8574},
]


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STATE_FILE = Path(os.environ.get("BOT_STATE_FILE", "telegram_balance_state.json"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
BALANCE_ALERT_USD = float(os.environ.get("BALANCE_ALERT_USD", "100"))
BALANCE_ALERT_PERCENT = float(os.environ.get("BALANCE_ALERT_PERCENT", "0.25"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "12"))
SEND_STARTUP_SUMMARY = os.environ.get("SEND_STARTUP_SUMMARY", "1") == "1"
SSL_VERIFY = os.environ.get("SSL_VERIFY", "1") != "0"
PRICE_CACHE_SECONDS = int(os.environ.get("PRICE_CACHE_SECONDS", "240"))
RATE_LIMIT_BACKOFF_SECONDS = int(os.environ.get("RATE_LIMIT_BACKOFF_SECONDS", "900"))

RUNNING = True


def default_state() -> dict:
    return {
        "chat_ids": [],
        "telegram_offset": 0,
        "last_alert_total": None,
        "last_seen_total": None,
        "last_prices": {},
        "previous_prices": {},
        "last_prices_at": 0,
        "previous_prices_at": 0,
        "rate_limited_until": 0,
        "custom_balance_alert_usd": None,
        "custom_balance_alert_percent": None,
        "started_at": now_iso(),
    }


def stop(_signum, _frame) -> None:
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return default_state()

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    merged = default_state()
    merged.update(state)
    return merged


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def create_ssl_context() -> ssl.SSLContext:
    if not SSL_VERIFY:
        return ssl._create_unverified_context()

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = create_ssl_context()


def http_json(url: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    data = None
    headers = {"User-Agent": "BAUMBalanceBot/1.0"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_api(method: str, payload: dict | None = None) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    return http_json(url, method="POST" if payload is not None else "GET", payload=payload)


def send_message(chat_id: int, text: str) -> None:
    telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def broadcast(state: dict, text: str) -> None:
    for chat_id in state.get("chat_ids", []):
        try:
            send_message(chat_id, text)
        except Exception as error:
            print(f"Failed to send message to {chat_id}: {error}", file=sys.stderr)


def format_usd(value: float) -> str:
    return f"${value:,.2f}"


def format_signed_usd(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def format_percent(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_number(value: float) -> str:
    return f"{value:,.8g}"


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def get_thresholds(state: dict) -> tuple[float, float]:
    usd = state.get("custom_balance_alert_usd")
    pct = state.get("custom_balance_alert_percent")
    return (
        float(usd) if usd is not None else BALANCE_ALERT_USD,
        float(pct) if pct is not None else BALANCE_ALERT_PERCENT,
    )


def wrap_code_block(lines: list[str]) -> str:
    return "<pre>" + "\n".join(lines) + "</pre>"


def divider() -> str:
    return "────────────"


def fetch_prices(state: dict | None = None, *, force: bool = False) -> dict:
    state = state if state is not None else {}
    now = time.time()
    cached_prices = state.get("last_prices") or {}
    cached_at = float(state.get("last_prices_at") or 0)
    rate_limited_until = float(state.get("rate_limited_until") or 0)

    if not force and cached_prices and now - cached_at < PRICE_CACHE_SECONDS:
        return cached_prices

    if not force and cached_prices and now < rate_limited_until:
        return cached_prices

    ids = [asset["cg_id"] for asset in ASSETS if asset["cg_id"]]
    query = urllib.parse.urlencode({"ids": ",".join(ids), "vs_currencies": "usd"})
    try:
        data = http_json(f"https://api.coingecko.com/api/v3/simple/price?{query}")
    except HTTPError as error:
        if error.code == 429:
            state["rate_limited_until"] = now + RATE_LIMIT_BACKOFF_SECONDS
            if cached_prices:
                print("CoinGecko rate limited; using cached prices", file=sys.stderr)
                return cached_prices
        raise

    prices = {}
    for asset in ASSETS:
      cg_id = asset["cg_id"]
      if cg_id:
          price = data.get(cg_id, {}).get("usd")
          if isinstance(price, (int, float)):
              prices[cg_id] = float(price)
    if state.get("last_prices"):
        state["previous_prices"] = state.get("last_prices", {})
        state["previous_prices_at"] = state.get("last_prices_at", 0)
    state["last_prices"] = prices
    state["last_prices_at"] = now
    state["rate_limited_until"] = 0
    return prices


def calculate_balance(prices: dict) -> tuple[float, list[dict]]:
    total = 0.0
    rows = []

    for asset in ASSETS:
        if asset["cg_id"]:
            price = prices.get(asset["cg_id"])
            if price is None:
                raise RuntimeError(f"Missing price for {asset['ticker']}")
            value = asset["qty"] * price
        else:
            price = 1.0
            value = asset["qty"]

        total += value
        rows.append(
            {
                "ticker": asset["ticker"],
                "qty": asset["qty"],
                "price": price,
                "value": value,
            }
        )

    return total, rows


def balance_message(total: float, rows: list[dict], title: str = "BAUM balance") -> str:
    rows_sorted = sorted(rows, key=lambda row: row["value"], reverse=True)
    table = []
    for row in rows_sorted:
        alloc = (row["value"] / total) * 100 if total else 0
        table.append(
            f"{row['ticker']:<5} {alloc:>6.2f}%  {format_usd(row['value']):>12}"
        )
    return "\n".join(
        [
            f"<b>{title}</b>",
            f"<b>Total</b>  {format_usd(total)}",
            divider(),
            wrap_code_block(table),
        ]
    )


def should_alert(last_total: float | None, current_total: float, usd_threshold: float, pct_threshold: float) -> tuple[bool, float, float]:
    if last_total is None:
        return False, 0.0, 0.0

    diff = current_total - last_total
    pct = (diff / last_total) * 100 if last_total else 0.0
    return abs(diff) >= usd_threshold or abs(pct) >= pct_threshold, diff, pct


def price_changes_message(state: dict, rows: list[dict]) -> str:
    prev_prices = state.get("previous_prices") or {}
    if not prev_prices:
        return "<b>Drivers</b>\n<code>No previous snapshot yet</code>"

    changes = []
    for row in rows:
        asset = next((item for item in ASSETS if item["ticker"] == row["ticker"]), None)
        if not asset or not asset["cg_id"]:
            continue
        prev_price = prev_prices.get(asset["cg_id"])
        if prev_price is None:
            continue
        value_delta = asset["qty"] * (row["price"] - prev_price)
        if abs(value_delta) < 0.01:
            continue
        changes.append((abs(value_delta), row["ticker"], value_delta))

    if not changes:
        return "<b>Drivers</b>\n<code>No meaningful move since last check</code>"

    top = sorted(changes, reverse=True)[:3]
    lines = [f"{ticker:<5} {format_signed_usd(delta):>12}" for _, ticker, delta in top]
    return "<b>Drivers</b>\n" + wrap_code_block(lines)


def prices_message(state: dict, prices: dict) -> str:
    prev_prices = state.get("previous_prices") or {}
    rows = []
    for asset in ASSETS:
        if not asset["cg_id"]:
            continue
        price = prices.get(asset["cg_id"])
        if price is None:
            continue
        prev_price = prev_prices.get(asset["cg_id"])
        if prev_price:
            pct = ((price - prev_price) / prev_price) * 100
            delta_text = format_percent(pct)
        else:
            delta_text = "new"
        rows.append(f"{asset['ticker']:<5} {format_usd(price):>12}  {delta_text:>9}")
    return "\n".join(
        [
            "<b>BAUM prices</b>",
            divider(),
            wrap_code_block(rows),
        ]
    )


def check_balance(state: dict, *, force_summary: bool = False) -> None:
    prices = fetch_prices(state)
    total, rows = calculate_balance(prices)
    last_alert_total = state.get("last_alert_total")
    usd_threshold, pct_threshold = get_thresholds(state)

    if last_alert_total is None:
        state["last_alert_total"] = total
        state["last_seen_total"] = total
        save_state(state)
        if force_summary or SEND_STARTUP_SUMMARY:
            broadcast(state, balance_message(total, rows, "BAUM monitor started"))
        return

    alert, diff, pct = should_alert(last_alert_total, total, usd_threshold, pct_threshold)
    state["last_seen_total"] = total

    if alert:
        direction = "up" if diff >= 0 else "down"
        text = "\n".join(
            [
                f"<b>BAUM alert · balance {direction}</b>",
                divider(),
                wrap_code_block(
                    [
                        f"Now        {format_usd(total)}",
                        f"Change     {format_signed_usd(diff)}  {format_percent(pct)}",
                        f"Baseline   {format_usd(last_alert_total)}",
                    ]
                ),
                price_changes_message(state, rows),
            ]
        )
        broadcast(state, text)
        state["last_alert_total"] = total

    save_state(state)


def handle_command(state: dict, message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if chat_id not in state["chat_ids"]:
        state["chat_ids"].append(chat_id)
        save_state(state)

    command = text.split()[0].split("@")[0].lower()

    if command in {"/start", "/help"}:
        usd_threshold, pct_threshold = get_thresholds(state)
        send_message(
            chat_id,
            "\n".join(
                [
                    "<b>BAUM balance bot</b>",
                    divider(),
                    "<b>Commands</b>",
                    wrap_code_block(
                        [
                            "/balance          portfolio value",
                            "/prices           tracked prices",
                            "/status           monitor state",
                            "/setusd 100       usd threshold",
                            "/setpct 0.50      percent threshold",
                            "/resetthresholds  restore defaults",
                        ]
                    ),
                    f"<b>Alert threshold</b>  {format_usd(usd_threshold)} or {pct_threshold:.2f}%",
                ]
            ),
        )
        return

    if command == "/status":
        usd_threshold, pct_threshold = get_thresholds(state)
        rate_limited_until = float(state.get("rate_limited_until") or 0)
        rate_limit_text = "no"
        if rate_limited_until > time.time():
            rate_limit_text = f"yes, retry after {int(rate_limited_until - time.time())}s"
        last_prices_at = float(state.get("last_prices_at") or 0)
        cache_age = format_age(time.time() - last_prices_at) if last_prices_at else "none"
        send_message(
            chat_id,
            "\n".join(
                [
                    "<b>Monitor status</b>",
                    divider(),
                    wrap_code_block(
                        [
                            f"Interval    {CHECK_INTERVAL_SECONDS}s",
                            f"USD thr     {format_usd(usd_threshold)}",
                            f"PCT thr     {pct_threshold:.2f}%",
                            f"Cache       {PRICE_CACHE_SECONDS}s",
                            f"Snapshot    {cache_age}",
                            f"Rate limit  {rate_limit_text}",
                            f"Chats       {len(state.get('chat_ids', []))}",
                            f"Last total  {format_usd(state['last_seen_total']) if state.get('last_seen_total') else 'n/a'}",
                        ]
                    ),
                ]
            ),
        )
        return

    if command == "/prices":
        try:
            prices = fetch_prices(state)
            save_state(state)
            send_message(chat_id, prices_message(state, prices))
        except Exception as error:
            send_message(chat_id, f"Could not fetch prices: {error}")
        return

    if command == "/balance":
        try:
            prices = fetch_prices(state)
            total, rows = calculate_balance(prices)
            state["last_seen_total"] = total
            save_state(state)
            send_message(chat_id, balance_message(total, rows, "BAUM portfolio"))
        except Exception as error:
            send_message(chat_id, f"Could not fetch balance: {error}")
        return

    if command == "/setusd":
        try:
            value = float(text.split()[1])
            if value <= 0:
                raise ValueError
            state["custom_balance_alert_usd"] = value
            save_state(state)
            send_message(chat_id, f"<b>USD threshold updated</b>\n{divider()}\n<code>{format_usd(value)}</code>")
        except Exception:
            send_message(chat_id, "Usage: /setusd 100")
        return

    if command == "/setpct":
        try:
            value = float(text.split()[1])
            if value <= 0:
                raise ValueError
            state["custom_balance_alert_percent"] = value
            save_state(state)
            send_message(chat_id, f"<b>Percent threshold updated</b>\n{divider()}\n<code>{value:.2f}%</code>")
        except Exception:
            send_message(chat_id, "Usage: /setpct 0.50")
        return

    if command == "/resetthresholds":
        state["custom_balance_alert_usd"] = None
        state["custom_balance_alert_percent"] = None
        save_state(state)
        send_message(chat_id, "<b>Thresholds restored</b>\n" + divider())
        return

    send_message(chat_id, "Unknown command. Use /help.")


def poll_telegram(state: dict) -> None:
    payload = {
        "timeout": 10,
        "offset": state.get("telegram_offset", 0),
        "allowed_updates": ["message"],
    }
    data = telegram_api("getUpdates", payload)

    for update in data.get("result", []):
        state["telegram_offset"] = max(state.get("telegram_offset", 0), update["update_id"] + 1)
        message = update.get("message")
        if message:
            handle_command(state, message)

    save_state(state)


def main() -> None:
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    next_balance_check = 0.0

    print("BAUM Telegram balance bot started")
    while RUNNING:
        try:
            poll_telegram(state)
        except Exception as error:
            print(f"Telegram polling failed: {error}", file=sys.stderr)

        if time.time() >= next_balance_check:
            try:
                check_balance(state)
            except Exception as error:
                print(f"Balance check failed: {error}", file=sys.stderr)
            next_balance_check = time.time() + CHECK_INTERVAL_SECONDS

        time.sleep(1)

    save_state(state)
    print("BAUM Telegram balance bot stopped")


if __name__ == "__main__":
    main()
