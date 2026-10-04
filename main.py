import asyncio
import logging
import os
from os.path import splitext
from dotenv import load_dotenv 
import urllib.request
import yt_dlp
from PIL import Image

from telegram import ForceReply, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Token não definido no .env")

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

def get_message(update: Update):
    """Pega a mensagem do update, seja de chat normal ou de canal."""
    return update.message or update.channel_post or update.edited_channel_post

def format_upload_date(d):
    if not d or len(d) != 8:
        return None
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def build_caption(info_dict: dict) -> str:
    lines = []

    title = info_dict.get("title")
    if title:
        lines.append(f"🎵 • {title}")

    uploader = info_dict.get("uploader") or info_dict.get("channel")
    if uploader:
        lines.append(f"👤 • {uploader}")

    date = format_upload_date(info_dict.get("upload_date"))
    if date:
        lines.append(f"📅 • {date}")

    url = info_dict.get("webpage_url")
    if url:
        lines.append(f"🔗 • {url}")

    return "\n".join(lines)


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = get_message(update)
    if msg:
        await msg.reply_html(
            rf"Hi {user.mention_html()}!",
            reply_markup=ForceReply(selective=True),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = get_message(update)
    if msg:
        await msg.reply_text("Help!")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = get_message(update)
    if msg and msg.text:
        await msg.reply_text(msg.text)


def is_url(text: str) -> bool:
    text = text.strip().lower()
    return (
        text.startswith("http://")
        or text.startswith("https://")
        or "youtube.com/" in text
        or "youtu.be/" in text
    )


def build_ydl_opts() -> dict:
    return {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "outtmpl": "downloads/%(id)s.%(ext)s",
        "quiet": True,
        "keepvideo": False,
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10485760,  # 10 MB
        "js_runtimes": {"deno": {}},
    }


def download_thumbnail(info_dict: dict, chat_id: int, message_id: int):
    thumb_url = info_dict.get("thumbnail")
    if not thumb_url:
        return None

    temp_path = f"thumb_temp_{chat_id}_{message_id}.jpg"
    final_path = f"thumb_final_{chat_id}_{message_id}.jpg"

    try:
        urllib.request.urlretrieve(thumb_url, temp_path)
        img = Image.open(temp_path)
        img.thumbnail((320, 320))
        img = img.convert("RGB")
        img.save(final_path, "JPEG", quality=85)
        return final_path
    except Exception as e:
        logger.warning("Falha ao processar thumbnail: %s", e)
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


async def keep_typing(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia 'enviando áudio...' a cada 4s até ser cancelada."""
    try:
        while True:
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.UPLOAD_VOICE
            )
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def process_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source: str,
    target_chat_id: int,
    processing_msg=None,
    status_chat_id: int = None,
    delete_on_finish: bool = False,
) -> None:
    ydl_opts = build_ydl_opts()
    mp3_filename = None
    thumb_path = None

    msg = get_message(update)
    message_id = msg.message_id if msg else 0

    typing_task = None
    if status_chat_id:
        typing_task = asyncio.create_task(keep_typing(status_chat_id, context))

    try:
        os.makedirs("downloads", exist_ok=True)

        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(source, download=True)
                if info.get("_type") == "playlist":
                    entries = info.get("entries") or []
                    if not entries:
                        return None, None
                    info = entries[0]
                original = ydl.prepare_filename(info)
                base, _ = os.path.splitext(original)
                return info, base + ".mp3"

        info_dict, mp3_filename = await asyncio.to_thread(_download)

        if info_dict is None:
            if processing_msg:
                await processing_msg.edit_text("[e] Nenhum resultado encontrado.")
            return

        thumb_path = download_thumbnail(info_dict, target_chat_id, message_id)
        caption = build_caption(info_dict)

        audio_kwargs = {
            "chat_id": target_chat_id,
            "title": info_dict.get("title", "Áudio"),
            "performer": info_dict.get("uploader", "Desconhecido"),
            #"caption": caption,
        }
        if thumb_path:
            audio_kwargs["thumbnail"] = open(thumb_path, "rb")

        try:
            with open(mp3_filename, "rb") as audio_file:
                await context.bot.send_audio(audio=audio_file, **audio_kwargs)

            if processing_msg:
                if delete_on_finish:
                    await processing_msg.delete()
                else:
                    await processing_msg.edit_text(caption)

        finally:
            if thumb_path and "thumbnail" in audio_kwargs:
                audio_kwargs["thumbnail"].close()

    except Exception as e:
        logger.exception("Erro no process_and_send")
        if processing_msg:
            try:
                await processing_msg.edit_text(f"[e] {e}")
            except Exception:
                pass
    finally:
        if typing_task:
            typing_task.cancel()
        if mp3_filename and os.path.exists(mp3_filename):
            os.remove(mp3_filename)
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)


async def yt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = get_message(update)
    if msg is None:
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.id == CHANNEL_ID:
        text = (msg.text or "").strip()
        query = text[len("/yt"):].strip()
        if not query:
            return
    else:
        if not context.args:
            await msg.reply_text("[w] Uso: /yt <link ou nome da música>")
            return
        query = " ".join(context.args).strip()

    if is_url(query):
        source = query
        label = "[i] Baixando. Aguarde..."
    else:
        source = f"ytsearch1:{query}"
        label = "[i] Buscando. Aguarde..."

    processing_msg = await msg.reply_text(label)
    if chat.id == CHANNEL_ID:
        try:
            await msg.delete()
        except Exception as e:
            logger.warning("Não consegui apagar a msg do canal: %s", e)

        await process_and_send(
            update,
            context,
            source,
            target_chat_id=CHANNEL_ID,
            processing_msg=processing_msg,
            status_chat_id=chat.id,
            delete_on_finish=False,
        )
        return

    await process_and_send(
        update,
        context,
        source,
        target_chat_id=chat.id,
        processing_msg=processing_msg,
        status_chat_id=chat.id,
        delete_on_finish=False,
    )


def main() -> None:
    application = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(60)
        .write_timeout(120)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS & filters.Regex(r"^/yt(\s|$)"),
            yt,
        )
    )

    application.add_handler(CommandHandler("yt", yt))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Chat(CHANNEL_ID),
            echo,
        )
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
