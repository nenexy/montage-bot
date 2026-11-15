"""Telegram-бот автомонтажа: кидаешь видео — получаешь смонтированный ролик."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

from billing import Billing, Pack, find_pack, parse_packs, plural, receipt_data
from montage import Settings, fit_to_size, process
from montage.ffmpeg import FFmpegError, probe

load_dotenv()
log = logging.getLogger("montage-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOCAL_API = os.getenv("TELEGRAM_API_URL")  # свой Bot API сервер снимает лимит 20 МБ
MB = 1024 * 1024
DOWNLOAD_LIMIT = (2000 if LOCAL_API else 20) * MB
UPLOAD_LIMIT = (2000 if LOCAL_API else 50) * MB
MAX_DURATION = int(os.getenv("MAX_DURATION", "300"))
MAX_PHOTOS = 10
DATA = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data")).resolve()
jobs = asyncio.Semaphore(int(os.getenv("WORKERS", "1")))

# --- оплата через ЮKassa (Telegram Payments) ---
PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()
PAYMENTS = bool(PROVIDER_TOKEN)
PACKS = parse_packs(os.getenv("PACKS", "1:99,5:399,20:1290"))
FREE_VIDEOS = int(os.getenv("FREE_VIDEOS", "1"))
RECEIPT = os.getenv("YOOKASSA_RECEIPT", "0") == "1"  # чек 54-ФЗ: спросим e-mail и передадим в ЮKassa
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SUPPORT = os.getenv("SUPPORT_CONTACT", "")
billing = Billing(DATA / "bot.db", free_videos=FREE_VIDEOS)

OPTIONS = {
    "cut_pauses": "✂️ Вырезать паузы",
    "zoom_cuts": "🔍 Зум на склейках",
    "subtitles": "📝 Субтитры",
    "music": "🎵 Фоновая музыка",
    "sfx": "💨 Звуки переходов",
    "photos": "🖼 Фото-вставки",
}


@dataclass
class UserState:
    photos: list[Path] = field(default_factory=list)
    music: Path | None = None
    opts: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(OPTIONS, True))
    busy: bool = False
    pending: Message | None = None  # видео, которое ждёт оплаты


users: dict[int, UserState] = {}
router = Router()


def state(uid: int) -> UserState:
    return users.setdefault(uid, UserState())


def user_dir(uid: int) -> Path:
    d = DATA / "users" / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_kb(st: UserState) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{'✅' if st.opts[k] else '❌'} {title}", callback_data=f"opt:{k}")]
        for k, title in OPTIONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELP = (
    "🎬 <b>Бот-монтажёр</b>\n\n"
    f"Пришли видео до {MAX_DURATION // 60} минут — я:\n"
    "• вырежу паузы и тишину\n"
    "• добавлю зум на склейках\n"
    "• сделаю субтитры с подсветкой слов\n"
    "• наложу фоновую музыку и звуки на переходах\n"
    "• вставлю твои фото с анимацией\n\n"
    "<b>Как пользоваться</b>\n"
    "1. (необязательно) Пришли фото — они появятся в ролике\n"
    "2. (необязательно) Пришли аудио — это будет фоновая музыка\n"
    "3. Пришли видео и подожди пару минут\n\n"
    "/settings — что включить или выключить\n"
    "/reset — убрать загруженные фото и музыку"
)


def balance_text(uid: int) -> str:
    free, credits = billing.balance(uid)
    parts = []
    if free:
        parts.append(f"бесплатных: <b>{free}</b>")
    parts.append(f"оплаченных: <b>{credits}</b>")
    return "💳 Твой баланс — " + ", ".join(parts)


def packs_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p.title} — {p.rub} ₽" + _per_video(p), callback_data=f"buy:{p.payload}")]
        for p in PACKS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _per_video(p: Pack) -> str:
    return f" ({p.rub / p.credits:.0f} ₽/шт)" if p.credits > 1 else ""


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    text = HELP
    if PAYMENTS:
        free = f"Первые {FREE_VIDEOS} — бесплатно, дальше" if FREE_VIDEOS else "Стоимость:"
        cheapest = min(p.rub / p.credits for p in PACKS)
        text += f"\n/buy — купить ролики\n/balance — баланс\n\n💰 {free} от {cheapest:.0f} ₽ за ролик."
    await message.answer(text)


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    await message.answer(balance_text(message.from_user.id))


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    if not PAYMENTS:
        await message.answer("Оплата пока не подключена — бот работает бесплатно 🙂")
        return
    await message.answer(balance_text(message.from_user.id) + "\n\nВыбери пакет:", reply_markup=packs_kb())


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(
        "📄 <b>Условия</b>\n\n"
        "Оплата — за обработку видео ботом. 1 ролик = 1 смонтированное видео до "
        f"{MAX_DURATION // 60} мин. Купленные ролики не сгорают. Если монтаж не удался, "
        "ролик автоматически возвращается на баланс. Вопросы и возвраты — /paysupport."
    )


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    contact = f" Пиши: {SUPPORT}" if SUPPORT else ""
    await message.answer(f"🛟 Проблема с оплатой? Опиши, что случилось, и приложи время платежа.{contact}")


@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_stats(message: Message) -> None:
    s = billing.stats()
    await message.answer(
        f"👥 Пользователей: {s['users']}\n🎬 Роликов смонтировано: {s['videos']}\n"
        f"💰 Платежей: {s['payments']} на {s['revenue_rub']} ₽"
    )


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(cb: CallbackQuery, bot: Bot) -> None:
    pack = find_pack(PACKS, cb.data.removeprefix("buy:"))
    if not PAYMENTS or pack is None:
        await cb.answer("Этот пакет больше недоступен, открой /buy заново", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=cb.message.chat.id,
        title=f"Автомонтаж: {pack.title}",
        description=f"{pack.title} автомонтажа видео: паузы, субтитры, музыка, переходы. Не сгорают.",
        payload=pack.payload,
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=pack.title, amount=pack.rub * 100)],
        need_email=RECEIPT,
        send_email_to_provider=RECEIPT,
        provider_data=receipt_data(pack) if RECEIPT else None,
    )
    await cb.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    pack = find_pack(PACKS, query.invoice_payload)
    ok = pack is not None and query.currency == "RUB" and query.total_amount == pack.rub * 100
    if ok:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Цена изменилась — открой /buy и выбери пакет заново.")


@router.message(F.successful_payment)
async def on_paid(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    uid = message.from_user.id
    credits = int(sp.invoice_payload.split(":")[1])
    added = billing.add_payment(
        sp.telegram_payment_charge_id, sp.provider_payment_charge_id, uid, credits, sp.total_amount, sp.currency
    )
    log.info("payment uid=%s credits=%s amount=%s new=%s", uid, credits, sp.total_amount, added)
    await message.answer(f"✅ Оплата прошла, спасибо! +{credits} {plural(credits, 'ролик', 'ролика', 'роликов')}.\n"
                         + balance_text(uid))

    st = state(uid)
    if st.pending and not st.busy:
        pending, st.pending = st.pending, None
        kind = billing.try_spend(uid)
        if kind:
            await message.answer("🎬 Монтирую видео, которое ты присылал…")
            await run_job(pending, _media(pending), bot, st, kind)


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    await message.answer("Настройки монтажа:", reply_markup=settings_kb(state(message.from_user.id)))


@router.callback_query(F.data.startswith("opt:"))
async def toggle(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 1)[1]
    st = state(cb.from_user.id)
    if key in st.opts:
        st.opts[key] = not st.opts[key]
        await cb.message.edit_reply_markup(reply_markup=settings_kb(st))
    await cb.answer()


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    uid = message.from_user.id
    users[uid] = UserState(opts=state(uid).opts)
    shutil.rmtree(user_dir(uid), ignore_errors=True)
    await message.answer("Фото и музыка сброшены 👌")


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    st = state(message.from_user.id)
    if len(st.photos) >= MAX_PHOTOS:
        await message.answer(f"Максимум {MAX_PHOTOS} фото. /reset — начать заново.")
        return
    path = user_dir(message.from_user.id) / f"photo_{uuid.uuid4().hex[:8]}.jpg"
    await bot.download(message.photo[-1], destination=path)
    st.photos.append(path)
    await message.answer(f"🖼 Фото добавлено ({len(st.photos)}/{MAX_PHOTOS}). Теперь присылай видео.")


@router.message(F.audio | (F.document & F.document.mime_type.startswith("audio/")))
async def on_audio(message: Message, bot: Bot) -> None:
    media = message.audio or message.document
    if media.file_size and media.file_size > DOWNLOAD_LIMIT:
        await message.answer("Трек слишком большой для Telegram-бота (лимит 20 МБ).")
        return
    ext = Path(media.file_name or "track.mp3").suffix or ".mp3"
    path = user_dir(message.from_user.id) / f"music{ext}"
    await bot.download(media, destination=path)
    state(message.from_user.id).music = path
    await message.answer("🎵 Музыка сохранена — будет фоном в следующем ролике.")


@router.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
async def on_video(message: Message, bot: Bot) -> None:
    uid = message.from_user.id
    st = state(uid)
    media = _media(message)

    if st.busy:
        await message.answer("Я ещё монтирую твоё прошлое видео, подожди немного ⏳")
        return
    if media.file_size and media.file_size > DOWNLOAD_LIMIT:
        await message.answer(
            f"Файл {media.file_size / MB:.0f} МБ — Telegram даёт ботам скачивать только до "
            f"{DOWNLOAD_LIMIT // MB} МБ. Сожми видео или пришли покороче."
        )
        return
    duration = getattr(media, "duration", None)
    if duration and duration > MAX_DURATION:
        await message.answer(f"Видео длиннее {MAX_DURATION // 60} мин — пока так не умею.")
        return

    kind = None
    if PAYMENTS:
        kind = billing.try_spend(uid)
        if kind is None:
            st.pending = message
            await message.answer(
                "Бесплатные ролики закончились 🙂 Выбери пакет — после оплаты я сразу смонтирую это видео:",
                reply_markup=packs_kb(),
            )
            return
    await run_job(message, media, bot, st, kind)


def _media(message: Message):  # noqa: ANN202
    return message.video or message.video_note or message.document


async def run_job(message: Message, media, bot: Bot, st: UserState, kind: str | None) -> None:  # noqa: ANN001
    """Скачивает, монтирует и отправляет ролик. Если не вышло — возвращает ролик на баланс."""
    uid = message.from_user.id
    delivered = False
    st.busy = True
    status = await message.answer("⏳ Принял видео, ставлю в очередь…")
    loop = asyncio.get_running_loop()

    def on_stage(text: str) -> None:  # вызывается из рабочего потока
        asyncio.run_coroutine_threadsafe(_edit(status, text), loop)

    job = Path(tempfile.mkdtemp(prefix="job_", dir=DATA))
    try:
        async with jobs:
            await _edit(status, "⬇️ Скачиваю видео…")
            src = job / "input.mp4"
            await bot.download(media, destination=src)

            info = await asyncio.to_thread(probe, src)
            if info.duration > MAX_DURATION + 1:
                await _edit(status, f"Видео длиннее {MAX_DURATION // 60} мин — пока так не умею."
                                    + _refund_note(kind))
                return

            settings = Settings.from_env(**st.opts)
            out = job / "montage.mp4"
            result = await asyncio.to_thread(
                process, src, out, settings, list(st.photos), st.music, on_stage
            )
            await _edit(status, "📤 Отправляю…")
            final = await asyncio.to_thread(fit_to_size, out, UPLOAD_LIMIT - 2 * MB, result.duration_out)

        saved = result.duration_in - result.duration_out
        caption = (
            f"✅ Готово! {result.duration_in:.0f} с → {result.duration_out:.0f} с"
            + (f" (вырезал {saved:.0f} с пауз, склеек: {result.cuts})" if result.cuts else "")
        )
        if kind:
            free, credits = billing.balance(uid)
            caption += f"\nОсталось роликов: {free + credits}" + (" · /buy" if free + credits == 0 else "")
        await message.answer_video(FSInputFile(final, filename="montage.mp4"), caption=caption,
                                   supports_streaming=True)
        delivered = True
        billing.mark_done(uid)
        await status.delete()
    except (FFmpegError, ValueError) as e:
        log.exception("montage failed")
        await _edit(status, f"😕 Не получилось смонтировать: {str(e).splitlines()[-1][:300]}" + _refund_note(kind))
    except Exception:
        log.exception("unexpected error")
        await _edit(status, "😕 Что-то сломалось при монтаже. Попробуй ещё раз или другое видео." + _refund_note(kind))
    finally:
        if kind and not delivered:
            billing.refund(uid, kind)
        st.busy = False
        shutil.rmtree(job, ignore_errors=True)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Пришли видео, фото или музыку. /help — подробности.")


def _refund_note(kind: str | None) -> str:
    return "\n↩️ Ролик вернул на баланс." if kind else ""


async def _edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:  # «message is not modified» и т.п. — не критично
        pass


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN — впиши токен от @BotFather в файл .env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    DATA.mkdir(parents=True, exist_ok=True)

    session = None
    if LOCAL_API:
        session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API, is_local=True))
    bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="start", description="Как пользоваться"),
        BotCommand(command="settings", description="Что включить/выключить"),
        BotCommand(command="reset", description="Сбросить фото и музыку"),
        *([BotCommand(command="buy", description="Купить ролики"),
           BotCommand(command="balance", description="Мой баланс"),
           BotCommand(command="terms", description="Условия"),
           BotCommand(command="paysupport", description="Помощь с оплатой")] if PAYMENTS else []),
    ])
    log.info("Оплата: %s", f"ЮKassa, пакеты {PACKS}" if PAYMENTS else "выключена (нет PAYMENT_PROVIDER_TOKEN)")
    dp = Dispatcher()
    dp.include_router(router)

    if os.getenv("PRELOAD_WHISPER", "1") == "1":  # грузим модель заранее, чтобы первый ролик не ждал
        from faster_whisper import WhisperModel

        from montage.subtitles import _models

        s = Settings.from_env()
        log.info("Загружаю Whisper %s…", s.whisper_model)
        _models[(s.whisper_model, s.whisper_device)] = await asyncio.to_thread(
            WhisperModel, s.whisper_model, device=s.whisper_device, compute_type="int8"
        )

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
