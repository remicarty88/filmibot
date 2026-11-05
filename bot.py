"""
Телеграм-бот: поиск и просмотр фильмов.

Переиспользует логику server.py (поиск Киного, карточка фильма, прямые потоки),
поэтому отдельный сервер поднимать не нужно — бот работает сам.

Запуск:
    python bot.py

Просмотр:
    * если у фильма есть прямой mp4   -> бот отправляет видео в чат;
    * иначе (HLS/DASH) и для сериалов -> кнопка "Открыть в плеере" (веб-плеер server.py).

Переменные окружения:
    BOT_TOKEN     - токен бота (есть значение по умолчанию, лучше задавать в env)
    WEB_BASE_URL  - публичный адрес веб-плеера (server.py), напр. https://xxx.up.railway.app
                    без него ссылки на плеер и видео через прокси не отдаются
    KINOGO_ORIGIN / HDREZKA_ORIGIN / HDREZKA_PROXY / PROXY_GATEWAY / VIDEO_PROXY
                  - пробрасываются в server.py
"""
import asyncio
import html
import logging
import os
from urllib.parse import urlencode, quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import server as S

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("kino88-bot")

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "9126447892:AAGcNF_9xjJ7yvsogf3yEmYDXss-7c3AGDs"
).strip()
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "").strip().rstrip("/")
if not WEB_BASE_URL:
    # на Railway публичный домен подставляется автоматически
    _d = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if _d:
        WEB_BASE_URL = "https://" + _d
PAGE_SIZE = 6
CAPTION_LIMIT = 900

HELP_TEXT = (
    "🎬 <b>Фильмы - 88</b>\n\n"
    "Просто отправь название фильма или сериала — найду и дам посмотреть.\n"
    "Полностью бесплатно.\n\n"
    "<b>Как смотреть:</b>\n"
    "• есть прямой mp4 — пришлю видео прямо в чат;\n"
    "• HLS/DASH или сериал — открою веб-плеер."
)


# ------------------------------------------------------------------ утилиты
def esc(t) -> str:
    return html.escape(str(t or ""))


def player_url(film_url, season=None, episode=None):
    """Ссылка на веб-плеер с глубокой привязкой к фильму/серии."""
    if not WEB_BASE_URL or not film_url:
        return None
    params = {"u": film_url}
    if season is not None:
        params["s"] = season
    if episode is not None:
        params["e"] = episode
    return WEB_BASE_URL + "/?" + urlencode(params)


def btn(text, **kw):
    return InlineKeyboardButton(text, **kw)


async def edit_panel(query, text, kb):
    """Заменяет содержимое текущей карточки, будь то фото или текст."""
    try:
        await query.edit_message_caption(
            caption=text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return
    except Exception:
        pass
    try:
        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ------------------------------------------------------------------ выдача
def results_kb(results, page):
    start = page * PAGE_SIZE
    chunk = results[start:start + PAGE_SIZE]
    rows = []
    for i, r in enumerate(chunk):
        title = r.get("title") or "—"
        title = title if len(title) <= 45 else title[:42] + "…"
        rows.append([btn(f"{start + i + 1}. {title}", callback_data=f"r:{start + i}")])
    pages = (len(results) - 1) // PAGE_SIZE + 1
    nav = []
    if page > 0:
        nav.append(btn("◀️", callback_data=f"pg:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", callback_data="noop"))
    if start + PAGE_SIZE < len(results):
        nav.append(btn("▶️", callback_data=f"pg:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def show_results(message, ctx, page=0):
    results = ctx.user_data.get("results") or []
    q = ctx.user_data.get("q", "")
    if not results:
        await message.reply_text("Ничего не найдено. Попробуй другое название.")
        return
    text = f"🔎 <b>{esc(q)}</b>\nНашлось: {len(results)}\n\nВыбери, что смотреть:"
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML,
                                reply_markup=results_kb(results, page))
    except Exception:
        await message.reply_text(text, parse_mode=ParseMode.HTML,
                                 reply_markup=results_kb(results, page))


def film_caption(film):
    name = film.get("name") or "—"
    bits = []
    if film.get("releaseYear"):
        bits.append(str(film["releaseYear"]))
    bits.append("сериал" if film.get("isSeries") else "фильм")
    r = (film.get("rating") or {}).get("value")
    if r is not None:
        bits.append(f"★ {r}")
    head = f"<b>{esc(name)}</b>\n<i>{esc(' • '.join(bits))}</i>"
    desc = (film.get("description") or "").strip()
    if desc:
        if len(desc) > CAPTION_LIMIT:
            desc = desc[:CAPTION_LIMIT] + "…"
        head += "\n\n" + esc(desc)
    return head


def film_kb(film, ctx):
    rows = []
    if film.get("isSeries") and film.get("seasons"):
        rows.append([btn("📺 Выбрать серию", callback_data="ss")])
    elif any(s.get("mp4") for s in film.get("sources", [])):
        rows.append([btn("▶️ Смотреть видео", callback_data="v")])
    pu = player_url(ctx.user_data.get("film_url"))
    if pu:
        rows.append([btn("🌐 Открыть в плеере", url=pu)])
    return InlineKeyboardMarkup(rows) if rows else None


async def show_film(message, ctx, url):
    await message.chat.send_action(ChatAction.TYPING)
    try:
        film = await asyncio.to_thread(S.kinogo_film, url)
    except Exception as e:
        await message.reply_text(f"⚠️ Не удалось открыть фильм: {type(e).__name__}: {e}")
        return
    ctx.user_data["film"] = film
    ctx.user_data["film_url"] = url
    ctx.user_data.pop("play", None)
    caption = film_caption(film)
    kb = film_kb(film, ctx)
    thumb = film.get("thumbnail")
    if thumb:
        try:
            await message.reply_photo(photo=thumb, caption=caption,
                                      parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception:
            pass
    await message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb)


def seasons_kb(seasons):
    rows, row = [], []
    for s in seasons:
        label = s.get("season_text") or f"Сезон {s.get('season')}"
        row.append(btn(label, callback_data=f"se:{s.get('season')}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("⬅️ К фильму", callback_data="film")])
    return InlineKeyboardMarkup(rows)


def episodes_kb(film, season):
    se = next((x for x in film.get("seasons", [])
               if str(x.get("season")) == str(season)), None)
    if not se:
        return None
    rows, row = [], []
    for e in se.get("episodes", []):
        row.append(btn(str(e.get("episode")), callback_data=f"ep:{season}:{e.get('episode')}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("⬅️ Сезоны", callback_data="ss")])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ поиск
async def do_search(message, ctx, query):
    ctx.user_data["q"] = query
    await message.chat.send_action(ChatAction.TYPING)
    try:
        results = await asyncio.to_thread(S.kinogo_search, query)
    except Exception as e:
        await message.reply_text(f"⚠️ Ошибка поиска: {type(e).__name__}: {e}")
        return
    ctx.user_data["results"] = [r for r in results if r.get("url")]
    if not ctx.user_data["results"]:
        await message.reply_text("Ничего не найдено. Попробуй другое название.")
        return
    await show_results(message, ctx, 0)


# ------------------------------------------------------------------ видео
async def send_video(query, ctx):
    play = ctx.user_data.get("play") or {}
    mp4 = play.get("mp4")
    if not mp4:
        for src in (ctx.user_data.get("film") or {}).get("sources", []):
            if src.get("mp4"):
                mp4 = src["mp4"]
                break
    if not mp4:
        await query.answer("Для этого видео прямого mp4 нет — открой в плеере.", show_alert=True)
        return

    await query.answer("Отправляю видео…")
    await query.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    url = mp4
    if WEB_BASE_URL:
        url = f"{WEB_BASE_URL}/api/proxy?url={quote(mp4, safe='')}"
    name = (ctx.user_data.get("film") or {}).get("name") or "Видео"
    try:
        await query.message.reply_video(
            video=url, caption=esc(name), supports_streaming=True,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning("send_video failed: %s", e)
        pu = player_url(ctx.user_data.get("film_url"))
        if pu:
            await query.message.reply_text(
                "Не удалось отправить видео. Смотри в плеере:",
                reply_markup=InlineKeyboardMarkup([[btn("🌐 Смотреть", url=pu)]]),
            )
        else:
            await query.message.reply_text(
                f"⚠️ Не удалось отправить видео: {type(e).__name__}: {e}"
            )


# ------------------------------------------------------------------ хендлеры
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    low = text.lower()
    if low.startswith("http") and ("kinogo" in low or "hdrezka" in low):
        await show_film(update.effective_message, ctx, text)
        return
    if low.startswith("/"):
        return
    await do_search(update.effective_message, ctx, text)


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data == "noop":
        return

    if data.startswith("pg:"):
        await show_results(q.message, ctx, int(data.split(":")[1]))
        return

    if data.startswith("r:"):
        idx = int(data.split(":")[1])
        results = ctx.user_data.get("results") or []
        if 0 <= idx < len(results):
            await show_film(q.message, ctx, results[idx]["url"])
        return

    if data == "ss":
        film = ctx.user_data.get("film") or {}
        if film.get("seasons"):
            await edit_panel(q, "📺 <b>Выбери сезон</b>", seasons_kb(film["seasons"]))
        return

    if data == "film":
        film = ctx.user_data.get("film") or {}
        await edit_panel(q, film_caption(film), film_kb(film, ctx))
        return

    if data.startswith("se:"):
        film = ctx.user_data.get("film") or {}
        season = data.split(":")[1]
        kb = episodes_kb(film, season)
        if kb:
            await edit_panel(q, f"📺 <b>{esc(season)} сезон — выбери серию</b>", kb)
        return

    if data.startswith("ep:"):
        _, season, episode = data.split(":", 2)
        film = ctx.user_data.get("film") or {}
        se = next((x for x in film.get("seasons", [])
                   if str(x.get("season")) == str(season)), None)
        ep = None
        if se:
            ep = next((x for x in se.get("episodes", [])
                       if str(x.get("episode")) == str(episode)), None)
        if not ep:
            return
        ctx.user_data["play"] = {"mp4": ep.get("mp4"), "hls": ep.get("hls")}
        rows = []
        if ep.get("mp4"):
            rows.append([btn("▶️ Смотреть видео", callback_data="v")])
        pu = player_url(ctx.user_data.get("film_url"), season, episode)
        if pu:
            rows.append([btn("🌐 Открыть в плеере", url=pu)])
        kb = InlineKeyboardMarkup(rows) if rows else None
        text = f"📺 <b>{esc(season)} сезон, серия {esc(episode)}</b>"
        if kb:
            await edit_panel(q, text, kb)
        else:
            await q.message.reply_text(
                text + "\n\nПлеер недоступен: задай WEB_BASE_URL на сервере.",
                parse_mode=ParseMode.HTML,
            )
        return

    if data == "v":
        await send_video(q, ctx)
        return


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Ловит ошибки сети/polling, чтобы не заливать лог трейсбеком."""
    err = ctx.error
    if isinstance(err, Conflict):
        log.warning(
            "409 Conflict: Telegram опрашивают сразу несколько экземпляров бота "
            "(вторая реплика Railway или запущенный локально bot.py). "
            "Оставьте ровно один — бот сам восстановится, когда лишний процесс умрёт."
        )
        return
    if isinstance(err, TelegramError):
        log.warning("Ошибка Telegram API: %s", err)
        return
    log.exception("Ошибка обработки апдейта", exc_info=err)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Бот запущен. Плеер: %s", WEB_BASE_URL or "(WEB_BASE_URL не задан)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
