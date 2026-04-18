from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image
import pytesseract
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
TRADES_PATH = DATA_DIR / "trades.json"
PUBLIC_DIR = BASE_DIR / "public"
HTML_PATH = PUBLIC_DIR / "index.html"


FIELD_ALIASES = {
    "канал": "channel_name",
    "channel": "channel_name",
    "монета": "pair",
    "тикер": "ticker",
    "pair": "pair",
    "сторона": "side",
    "side": "side",
    "вход": "my_entry_price",
    "мой вход": "my_entry_price",
    "entry": "my_entry_price",
    "стоп": "stop_loss",
    "stop": "stop_loss",
    "tp1": "tp1",
    "tp2": "tp2",
    "tp3": "tp3",
    "плечо": "my_leverage",
    "leverage": "my_leverage",
    "сигнал плечо": "signal_leverage",
    "комментарий": "notes",
    "notes": "notes",
}


def load_env() -> str:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    return token


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    if not TRADES_PATH.exists():
        TRADES_PATH.write_text("[]", encoding="utf-8")


def load_trades() -> list[dict[str, Any]]:
    ensure_dirs()
    try:
        return json.loads(TRADES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_trades(trades: list[dict[str, Any]]) -> None:
    TRADES_PATH.write_text(
        json.dumps(trades, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_caption(caption: str | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not caption:
        return parsed

    for raw_line in caption.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        mapped_key = FIELD_ALIASES.get(key.strip().lower())
        if mapped_key:
            parsed[mapped_key] = value.strip()

    pair = parsed.get("pair", "").upper()
    ticker = parsed.get("ticker", "").upper()
    side = parsed.get("side", "").lower()

    if pair and not ticker:
        parsed["ticker"] = pair.replace("USDT", "").replace("USD", "")
    if ticker and not pair:
        parsed["pair"] = f"{ticker}USDT"
    if side in {"лонг", "long"}:
        parsed["side"] = "long"
    if side in {"шорт", "short"}:
        parsed["side"] = "short"
    return parsed


def normalize_ocr_text(text: str) -> str:
    replacements = {
        "Кросс": "кросс",
        "Шорт": "шорт",
        "Лонг": "лонг",
        "Стоп": "стоп",
        "Цена входа": "цена входа",
    }
    normalized = text
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def extract_first_decimal(text: str) -> str:
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return ""
    return match.group(0).replace(",", ".")


def extract_signal_data_from_ocr(text: str) -> dict[str, str]:
    normalized = normalize_ocr_text(text)
    parsed: dict[str, str] = {}

    pair_match = re.search(r"\b([A-Z]{2,12}USDT)\b", text)
    if pair_match:
        parsed["pair"] = pair_match.group(1).upper()
        parsed["ticker"] = parsed["pair"].replace("USDT", "")

    if "шорт" in normalized or "short" in normalized.lower():
        parsed["side"] = "short"
    elif "лонг" in normalized or "long" in normalized.lower():
        parsed["side"] = "long"

    leverage_match = re.search(r"(?:кросс|cross)\s*(\d{1,3}x)", normalized, flags=re.IGNORECASE)
    if leverage_match:
        parsed["signal_leverage"] = leverage_match.group(1).lower()

    stop_match = re.search(r"(?:стоп|stop)\s+(\d+(?:[.,]\d+)?)", normalized, flags=re.IGNORECASE)
    if stop_match:
        parsed["stop_loss"] = stop_match.group(1).replace(",", ".")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if "подписчиков" in line.lower() and index > 0:
            parsed["channel_name"] = lines[index - 1]
            break

    price_anchor_found = False
    for line in lines:
        lowered = line.lower()
        if "цена входа" in lowered:
            price_anchor_found = True
            continue
        if price_anchor_found:
            decimals = re.findall(r"\d+(?:[.,]\d+)?", line)
            if decimals:
                parsed["entry_price"] = decimals[1 if len(decimals) > 1 else 0].replace(",", ".")
                break

    return parsed


def read_ocr_text(image_path: Path) -> str:
    image = Image.open(image_path)
    return pytesseract.image_to_string(image, lang="eng+rus")


def channel_rating(channel_name: str, trades: list[dict[str, Any]]) -> float:
    scores = [
        float(item.get("channel_cleanliness_score", 0) or 0)
        for item in trades
        if item.get("channel_name") == channel_name and item.get("channel_cleanliness_score")
    ]
    if not scores:
        return 5.0
    return round(sum(scores) / len(scores), 1)


def render_html(trades: list[dict[str, Any]]) -> str:
    channels: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        channels.setdefault(trade.get("channel_name", "Не указан"), []).append(trade)

    total_signals = len(trades)
    open_trades = sum(1 for item in trades if item.get("status", "open") == "open")
    avg_cleanliness_values = [
        float(item.get("channel_cleanliness_score", 0) or 0)
        for item in trades
        if item.get("channel_cleanliness_score")
    ]
    avg_cleanliness = (
        round(sum(avg_cleanliness_values) / len(avg_cleanliness_values), 1)
        if avg_cleanliness_values
        else 0.0
    )

    channel_sections = []
    for channel_name, items in channels.items():
        rating = channel_rating(channel_name, trades)
        avg_signal_quality_values = [
            float(item.get("signal_quality_score", 0) or 0)
            for item in items
            if item.get("signal_quality_score")
        ]
        avg_signal_quality = (
            round(sum(avg_signal_quality_values) / len(avg_signal_quality_values), 1)
            if avg_signal_quality_values
            else 0.0
        )
        wins = sum(1 for item in items if item.get("outcome") == "win")
        losses = sum(1 for item in items if item.get("outcome") == "loss")
        closed = sum(1 for item in items if item.get("status") == "closed")
        winrate = f"{round((wins / closed) * 100, 1)}%" if closed else "—"

        cards = []
        for item in sorted(items, key=lambda x: x.get("recorded_at", ""), reverse=True):
            side = item.get("side", "").lower()
            side_badge = "long" if side == "long" else "short"
            result_value = item.get("result_usdt", "")
            result_class = "positive" if str(result_value).startswith("+") else "negative"
            result_display = f"{result_value} USDT" if result_value else "—"
            deviation = item.get("deviation_from_idea_pct") or "Ждет данных"
            adverse_price = item.get("max_adverse_price") or "—"
            cards.append(
                f"""
                <article class="trade-card">
                  <div class="trade-top">
                    <div class="trade-pair">{item.get("pair", "—")}</div>
                    <div class="badges">
                      <span class="badge {side_badge}">{item.get("side", "—").upper()}</span>
                      <span class="badge">Сигнал: {item.get("signal_leverage", "—") or "—"}</span>
                      <span class="badge">Ваше плечо: {item.get("my_leverage", "—") or "—"}</span>
                      <span class="badge">Статус: {item.get("status", "open").upper()}</span>
                    </div>
                  </div>
                  <div class="trade-details">
                    <div class="detail"><div class="label">Дата сигнала</div><div class="value">{item.get("signal_date", "—")}</div></div>
                    <div class="detail"><div class="label">Вход по сигналу</div><div class="value">{item.get("entry_price", "—") or "—"}</div></div>
                    <div class="detail"><div class="label">Ваш вход</div><div class="value">{item.get("my_entry_price", "—") or "—"}</div></div>
                    <div class="detail"><div class="label">Стоп</div><div class="value">{item.get("stop_loss", "—") or "—"}</div></div>
                    <div class="detail"><div class="label">Результат</div><div class="value {result_class}">{result_display}</div></div>
                    <div class="detail"><div class="label">Качество сигнала</div><div class="value">{item.get("signal_quality_score", "—") or "—"}</div></div>
                    <div class="detail"><div class="label">Дисциплина</div><div class="value">{item.get("execution_discipline_score", "—") or "—"}</div></div>
                    <div class="detail"><div class="label">Отклонение от идеи</div><div class="value">{deviation}</div></div>
                    <div class="detail"><div class="label">Макс. ход против идеи</div><div class="value">{adverse_price}</div></div>
                  </div>
                  <div class="notes">
                    <div class="note"><strong>Комментарий</strong>{item.get("notes", "—") or "—"}</div>
                    <div class="note"><strong>Рыночный фон</strong>{item.get("btc_context", "—") or "—"}</div>
                    <div class="note"><strong>Аналитика</strong>{item.get("market_notes", "—") or "—"}</div>
                  </div>
                </article>
                """
            )

        channel_sections.append(
            f"""
            <article class="channel-card">
              <div class="channel-head">
                <div class="channel-name">{channel_name}</div>
                <div class="channel-rating">Оценка канала: {rating} / 10</div>
              </div>
              <div class="channel-meta">
                <div class="meta-box"><div class="label">Сигналов</div><div class="value">{len(items)}</div></div>
                <div class="meta-box"><div class="label">Закрыто</div><div class="value">{closed}</div></div>
                <div class="meta-box"><div class="label">Winrate</div><div class="value">{winrate}</div></div>
                <div class="meta-box"><div class="label">Среднее качество</div><div class="value">{avg_signal_quality or "—"}</div></div>
              </div>
              <div class="trade-grid">
                {''.join(cards)}
              </div>
            </article>
            """
        )

    if not channel_sections:
        channel_sections.append(
            """
            <article class="channel-card">
              <div class="channel-head">
                <div class="channel-name">Пока пусто</div>
                <div class="channel-rating">Оценка канала: —</div>
              </div>
              <div class="trade-grid">
                <article class="trade-card">
                  <div class="notes">
                    <div class="note"><strong>Журнал</strong>Отправьте первый скрин в Telegram-бота с подписью, и сделка появится здесь автоматически.</div>
                  </div>
                </article>
              </div>
            </article>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trade Journal</title>
  <style>
    :root {{
      --bg: #0a0c10;
      --panel: #12161d;
      --panel-2: #171c24;
      --muted: #8f99ab;
      --text: #f4f7fb;
      --line: rgba(255,255,255,0.08);
      --accent: #ff4d8d;
      --accent-2: #5be7c4;
      --warning: #ffc857;
      --danger: #ff6b6b;
      --shadow: 0 20px 60px rgba(0,0,0,0.45);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(255,77,141,0.18), transparent 30%),
        radial-gradient(circle at top right, rgba(91,231,196,0.12), transparent 25%),
        linear-gradient(180deg, #0a0c10 0%, #0f1319 100%);
      color: var(--text);
      min-height: 100vh;
    }}
    .wrap {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 56px; }}
    .hero {{
      display: grid; gap: 18px; padding: 28px; border: 1px solid var(--line); border-radius: 28px;
      background: linear-gradient(135deg, rgba(255,77,141,0.10), rgba(18,22,29,0.95) 38%, rgba(91,231,196,0.08));
      box-shadow: var(--shadow); margin-bottom: 24px;
    }}
    .eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: 0.18em; font-size: 12px; font-weight: 700; }}
    h1 {{ margin: 0; font-size: clamp(30px, 5vw, 56px); line-height: 0.95; letter-spacing: -0.04em; }}
    .hero p {{ margin: 0; color: #cfd7e4; max-width: 760px; font-size: 16px; line-height: 1.6; }}
    .top-stats, .channel-grid, .trade-grid {{ display: grid; gap: 16px; }}
    .top-stats {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 28px; }}
    .stat, .channel-card, .trade-card {{
      background: linear-gradient(180deg, rgba(23,28,36,0.95), rgba(18,22,29,0.96));
      border: 1px solid var(--line); border-radius: 22px; box-shadow: var(--shadow);
    }}
    .stat {{ padding: 18px 20px; }}
    .stat-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 10px; }}
    .stat-value {{ font-size: 30px; font-weight: 800; letter-spacing: -0.04em; }}
    .section-title {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 16px; }}
    .section-title h2 {{ margin: 0; font-size: 22px; letter-spacing: -0.03em; }}
    .section-title span {{ color: var(--muted); font-size: 13px; }}
    .channel-grid {{ margin-bottom: 28px; }}
    .channel-card {{ overflow: hidden; }}
    .channel-head {{
      display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 20px 22px 14px;
      border-bottom: 1px solid var(--line); background: linear-gradient(90deg, rgba(255,77,141,0.14), rgba(255,77,141,0));
    }}
    .channel-name {{ font-size: 28px; font-weight: 800; letter-spacing: -0.04em; }}
    .channel-rating {{
      padding: 10px 14px; border-radius: 999px; font-weight: 700; color: #111;
      background: linear-gradient(135deg, var(--warning), #ffd97d); white-space: nowrap;
    }}
    .channel-meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; padding: 18px 22px 0; }}
    .meta-box, .detail {{
      background: rgba(255,255,255,0.03); border: 1px solid var(--line); border-radius: 16px; padding: 12px 14px;
    }}
    .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 6px; }}
    .value {{ font-size: 20px; font-weight: 800; letter-spacing: -0.03em; }}
    .trade-grid {{ padding: 18px 22px 22px; }}
    .trade-card {{
      padding: 18px; border-radius: 20px;
      background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.015)), linear-gradient(180deg, rgba(16,19,25,0.96), rgba(18,22,29,0.96));
    }}
    .trade-top {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }}
    .trade-pair {{ font-size: 28px; font-weight: 800; letter-spacing: -0.04em; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .badge {{
      padding: 8px 12px; border-radius: 999px; font-size: 12px; font-weight: 700; border: 1px solid var(--line);
      background: rgba(255,255,255,0.04); color: #dfe5ef;
    }}
    .badge.short {{ color: #ffd3db; background: rgba(255,107,107,0.12); border-color: rgba(255,107,107,0.28); }}
    .badge.long {{ color: #cffff1; background: rgba(91,231,196,0.12); border-color: rgba(91,231,196,0.28); }}
    .trade-details {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .positive {{ color: var(--accent-2); }}
    .negative {{ color: var(--danger); }}
    .notes {{ display: grid; gap: 10px; margin-top: 10px; }}
    .note {{ padding: 14px 16px; border-radius: 16px; border: 1px solid var(--line); background: rgba(255,255,255,0.025); }}
    .note strong {{ display: block; color: var(--accent); margin-bottom: 6px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.14em; }}
    @media (max-width: 720px) {{
      .wrap {{ width: min(100% - 20px, 1180px); padding-top: 20px; }}
      .hero, .channel-head, .channel-meta, .trade-grid {{ padding-left: 16px; padding-right: 16px; }}
      .channel-name, .trade-pair {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="eyebrow">Trader Journal</div>
      <h1>Онлайн-дневник сигналов, который обновляется прямо с VPS.</h1>
      <p>Вы присылаете скрины в Telegram-бота, а сервер сохраняет сделку, считает отклонение от идеи и сразу обновляет этот журнал.</p>
    </section>
    <section class="top-stats">
      <div class="stat"><div class="stat-label">Всего сигналов</div><div class="stat-value">{total_signals}</div></div>
      <div class="stat"><div class="stat-label">Открытых сделок</div><div class="stat-value">{open_trades}</div></div>
      <div class="stat"><div class="stat-label">Каналов в работе</div><div class="stat-value">{len(channels)}</div></div>
      <div class="stat"><div class="stat-label">Средняя чистота</div><div class="stat-value">{avg_cleanliness} / 10</div></div>
    </section>
    <div class="section-title">
      <h2>Группировка по каналам</h2>
      <span>Оценка канала считается по накопленным записям</span>
    </div>
    <section class="channel-grid">
      {''.join(channel_sections)}
    </section>
  </div>
</body>
</html>"""


def rebuild_html() -> None:
    trades = load_trades()
    HTML_PATH.write_text(render_html(trades), encoding="utf-8")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Бот готов.\n\n"
        "Отправьте скрин с подписью, например:\n"
        "канал: Мысли Эмилии\n"
        "монета: TAOUSDT\n"
        "сторона: short\n"
        "вход: 258.15\n"
        "стоп: 281.09\n"
        "плечо: 50x\n"
        "комментарий: вход с телефона"
    )
    await update.message.reply_text(text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dirs()
    message = update.message
    if not message or not message.photo:
        return

    largest = message.photo[-1]
    telegram_file = await largest.get_file()
    screenshot_name = f"{message.date.strftime('%Y%m%d_%H%M%S')}_{largest.file_unique_id}.jpg"
    screenshot_path = SCREENSHOTS_DIR / screenshot_name
    await telegram_file.download_to_drive(custom_path=str(screenshot_path))

    parsed = parse_caption(message.caption)
    ocr_text = ""
    try:
        ocr_text = read_ocr_text(screenshot_path)
        ocr_parsed = extract_signal_data_from_ocr(ocr_text)
        for key, value in ocr_parsed.items():
            if value and not parsed.get(key):
                parsed[key] = value
    except Exception:
        ocr_text = ""
    now = datetime.now()

    trade = {
        "recorded_at": now.strftime("%Y-%m-%d %H:%M"),
        "channel_name": parsed.get("channel_name", "Не указан"),
        "signal_date": now.strftime("%Y-%m-%d"),
        "ticker": parsed.get("ticker", ""),
        "pair": parsed.get("pair", ""),
        "side": parsed.get("side", ""),
        "signal_leverage": parsed.get("signal_leverage", ""),
        "my_leverage": parsed.get("my_leverage", ""),
        "entry_price": parsed.get("entry_price", parsed.get("my_entry_price", "")),
        "stop_loss": parsed.get("stop_loss", ""),
        "tp1": parsed.get("tp1", ""),
        "tp2": parsed.get("tp2", ""),
        "tp3": parsed.get("tp3", ""),
        "my_entry_price": parsed.get("my_entry_price", ""),
        "max_adverse_price": "",
        "deviation_from_idea_pct": "",
        "my_exit_price": "",
        "my_exit_date": "",
        "result_usdt": "",
        "outcome": "open",
        "status": "open",
        "btc_context": "",
        "market_notes": ocr_text[:1200],
        "signal_quality_score": "",
        "execution_discipline_score": "",
        "channel_cleanliness_score": "",
        "notes": parsed.get("notes", ""),
        "screenshot_file": screenshot_name,
    }

    trades = load_trades()
    trades.append(trade)
    save_trades(trades)
    rebuild_html()

    await message.reply_text(
        "Сигнал записал в журнал.\n"
        f"Канал: {trade['channel_name']}\n"
        f"Пара: {trade['pair'] or 'Не указана'}\n"
        f"Ваш вход: {trade['my_entry_price'] or 'Не указан'}\n"
        "HTML на VPS обновлен."
    )


def main() -> None:
    ensure_dirs()
    rebuild_html()
    token = load_env()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
