import os
import re
import time
import uuid
import shutil
import threading
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# ================= ЗАГРУЗКА ТОКЕНА ИЗ .ENV =================
load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')

if not TOKEN:
    raise ValueError(
        "❌ ОШИБКА: Переменная BOT_TOKEN не найдена! "
        "Убедись, что в папке с ботом создан файл .env с строкой BOT_TOKEN=твой_токен"
    )

bot = telebot.TeleBot(TOKEN, threaded=True)

# ================= НАСТРОЙКИ =================
# Папка для временного хранения скачиваемых файлов
DOWNLOAD_DIR = 'downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Лимит размера файла для отправки через Telegram Bot API (49.5 МБ с запасом)
MAX_FILESIZE = int(49.5 * 1024 * 1024)

# Проверка наличия FFmpeg в системе (нужен для склейки 1080p и конвертации в MP3)
HAS_FFMPEG = shutil.which('ffmpeg') is not None

# Хранилище сессий выбора качества и активных загрузок пользователей
video_sessions = {}
active_downloads = set()
lock = threading.Lock()

# Регулярное выражение для распознавания ссылок YouTube (обычные видео, Shorts, музыка, youtu.be)
YT_REGEX = re.compile(
    r'(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch\?v=|shorts/|live/)|youtu\.be/)[\w\-]+'
)


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def format_duration(seconds):
    """Переводит секунды в красивый формат ЧЧ:ММ:СС или ММ:СС"""
    if not seconds:
        return "Неизвестно"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_views(views):
    """Форматирует количество просмотров"""
    if not views:
        return "—"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f} млн"
    if views >= 1_000:
        return f"{views / 1_000:.1f} тыс."
    return str(views)


def make_progress_bar(percent):
    """Рисует текстовую шкалу прогресса"""
    filled = int(percent // 10)
    return "█" * filled + "░" * (10 - filled)


def cleanup_files(prefix):
    """Удаляет все временные файлы конкретной загрузки с диска"""
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(prefix):
                fpath = os.path.join(DOWNLOAD_DIR, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
    except Exception:
        pass


def build_quality_keyboard(session_id):
    """Создает кнопки выбора формата и качества"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🎬 1080p (Full HD)", callback_data=f"dl|{session_id}|1080"),
        InlineKeyboardButton("🎬 720p (HD)", callback_data=f"dl|{session_id}|720"),
        InlineKeyboardButton("🎬 480p", callback_data=f"dl|{session_id}|480"),
        InlineKeyboardButton("🎬 360p (Быстро)", callback_data=f"dl|{session_id}|360"),
    )
    markup.add(
        InlineKeyboardButton("🎵 Только звук (Аудио)", callback_data=f"dl|{session_id}|audio"),
        InlineKeyboardButton("🖼 Скачать превью", callback_data=f"dl|{session_id}|thumb")
    )
    markup.add(InlineKeyboardButton("❌ Отмена", callback_data=f"dl|{session_id}|cancel"))
    return markup


# ================= ОБРАБОТЧИКИ КОМАНД =================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    ffmpeg_status = "✅ Активен (доступно 1080p и MP3)" if HAS_FFMPEG else "⚠️ Не установлен на сервере (работа в режиме готовых MP4/M4A)"
    text = (
        "👋 **Привет! Я бот для скачивания видео и музыки с YouTube.**\n\n"
        "📌 **Что я умею:**\n"
        "• Скачивать обычные видео и **YouTube Shorts**\n"
        "• Давать выбор качества (`1080p`, `720p`, `480p`, `360p`)\n"
        "• Извлекать чистый звук для прослушивания в фоне\n"
        "• Скачивать обложку (превью) видео в максимальном качестве\n\n"
        f"⚙️ **Статус FFmpeg на сервере:** {ffmpeg_status}\n"
        "📦 **Лимит Telegram для ботов:** до 50 МБ на один файл.\n\n"
        "🔗 **Просто отправь мне ссылку на YouTube-видео!**"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


# ================= ПОЛУЧЕНИЕ ССЫЛКИ И ИНФОРМАЦИИ О ВИДЕО =================
@bot.message_handler(content_types=['text'])
def handle_url(message):
    url = message.text.strip()

    if not YT_REGEX.search(url):
        bot.reply_to(
            message,
            "⚠️ Отправь корректную ссылку на видео или Shorts с YouTube.\n"
            "Пример: `https://youtu.be/...`",
            parse_mode="Markdown"
        )
        return

    user_id = message.from_user.id
    if user_id in active_downloads:
        bot.reply_to(message, "⏳ Дождись завершения твоей текущей загрузки!")
        return

    wait_msg = bot.reply_to(message, "🔍 Получаю информацию о видео, подожди пару секунд...")

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get('title', 'Без названия')
        uploader = info.get('uploader', 'Неизвестный автор')
        duration = info.get('duration', 0)
        views = info.get('view_count', 0)
        thumbnail = info.get('thumbnail')

        # Создаем короткий ID сессии (так как callback_data в Telegram ограничена 64 байтами)
        session_id = uuid.uuid4().hex[:8]
        with lock:
            if len(video_sessions) > 300:
                video_sessions.clear()
            video_sessions[session_id] = {
                'url': url,
                'title': title,
                'uploader': uploader,
                'duration': duration,
                'thumbnail': thumbnail,
                'user_id': user_id
            }

        caption = (
            f"🎬 {title}\n\n"
            f"👤 Канал: {uploader}\n"
            f"⏱ Длительность: {format_duration(duration)}\n"
            f"👁 Просмотры: {format_views(views)}\n\n"
            f"👇 Выбери нужное качество или формат:"
        )

        keyboard = build_quality_keyboard(session_id)

        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except Exception:
            pass

        if thumbnail:
            bot.send_photo(
                message.chat.id,
                photo=thumbnail,
                caption=caption,
                reply_markup=keyboard,
                reply_to_message_id=message.message_id
            )
        else:
            bot.send_message(
                message.chat.id,
                caption,
                reply_markup=keyboard,
                reply_to_message_id=message.message_id
            )

    except Exception as e:
        try:
            bot.edit_message_text(
                f"❌ Не удалось получить доступ к видео.\n"
                f"Возможно, видео приватное, удалено или YouTube заблокировал запрос с IP хостинга.\n\n"
                f"Детали: {str(e)[:120]}",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass


# ================= ОБРАБОТКА НАЖАТИЯ КНОПОК И СКАЧИВАНИЕ =================
@bot.callback_query_handler(func=lambda call: call.data.startswith('dl|'))
def handle_download_callback(call):
    _, session_id, choice = call.data.split('|')

    session = video_sessions.get(session_id)
    if not session:
        bot.answer_callback_query(call.id, "⚠️ Сессия устарела. Отправь ссылку заново!", show_alert=True)
        return

    if call.from_user.id != session['user_id']:
        bot.answer_callback_query(call.id, "⛔ Эта кнопка создана для другого пользователя.", show_alert=True)
        return

    if choice == 'cancel':
        bot.answer_callback_query(call.id, "Загрузка отменена")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if choice == 'thumb':
        bot.answer_callback_query(call.id, "Отправляю обложку...")
        if session.get('thumbnail'):
            bot.send_photo(
                call.message.chat.id,
                session['thumbnail'],
                caption=f"🖼 Обложка видео: {session['title']}"
            )
        else:
            bot.send_message(call.message.chat.id, "⚠️ Обложка для этого видео не найдена.")
        return

    user_id = call.from_user.id
    with lock:
        if user_id in active_downloads:
            bot.answer_callback_query(call.id, "⏳ Подожди, твоё предыдущее видео ещё качается!", show_alert=True)
            return
        active_downloads.add(user_id)

    bot.answer_callback_query(call.id, "🚀 Начинаю загрузку...")

    thread = threading.Thread(
        target=process_download,
        args=(call.message, session, choice)
    )
    thread.daemon = True
    thread.start()


def process_download(msg, session, choice):
    chat_id = msg.chat.id
    user_id = session['user_id']
    url = session['url']
    title = session['title']
    file_prefix = f"{user_id}_{uuid.uuid4().hex[:6]}"
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.%(ext)s")

    status_msg = bot.send_message(chat_id, f"⏳ Подготовка к скачиванию: «{title}»...")
    last_update_time = [0]

    def progress_hook(d):
        if d['status'] == 'downloading':
            now = time.time()
            if now - last_update_time[0] < 3.0:
                return

            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)

            if total > MAX_FILESIZE or downloaded > MAX_FILESIZE:
                raise Exception("FILE_TOO_LARGE")

            if total > 0:
                percent = (downloaded / total) * 100
                bar = make_progress_bar(percent)
                dl_mb = downloaded / (1024 * 1024)
                tot_mb = total / (1024 * 1024)
                speed = (d.get('speed') or 0) / (1024 * 1024)
                text = (
                    f"📥 Загрузка: {percent:.1f}%\n"
                    f"[{bar}]\n"
                    f"📦 {dl_mb:.1f} МБ из {tot_mb:.1f} МБ ({speed:.1f} МБ/с)"
                )
                try:
                    bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg.message_id)
                    last_update_time[0] = now
                except Exception:
                    pass

    try:
        ydl_opts = {
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'max_filesize': MAX_FILESIZE,
            'progress_hooks': [progress_hook],
        }

        if choice == 'audio':
            if HAS_FFMPEG:
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            else:
                ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
        else:
            height = int(choice)
            if HAS_FFMPEG:
                ydl_opts['format'] = (
                    f"bestvideo[height<={height}][filesize<{MAX_FILESIZE}]+bestaudio/"
                    f"best[height<={height}]/best"
                )
                ydl_opts['merge_output_format'] = 'mp4'
            else:
                ydl_opts['format'] = (
                    f"best[height<={height}][ext=mp4]/"
                    f"best[height<={height}]/best[ext=mp4]/best"
                )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded_file = None
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(file_prefix):
                downloaded_file = os.path.join(DOWNLOAD_DIR, fname)
                break

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise Exception("Файл не был создан (возможно, выбранный размер превышает 50 МБ).")

        file_size = os.path.getsize(downloaded_file)
        if file_size > MAX_FILESIZE:
            raise Exception("FILE_TOO_LARGE")

        try:
            bot.edit_message_text(
                "📤 Загрузка завершена! Отправляю файл в Telegram...",
                chat_id=chat_id,
                message_id=status_msg.message_id
            )
        except Exception:
            pass

        with open(downloaded_file, 'rb') as f:
            if choice == 'audio':
                bot.send_chat_action(chat_id, 'upload_audio')
                bot.send_audio(
                    chat_id,
                    f,
                    title=title,
                    performer=session.get('uploader', 'YouTube'),
                    caption=f"🎵 {title}"
                )
            else:
                bot.send_chat_action(chat_id, 'upload_video')
                bot.send_video(
                    chat_id,
                    f,
                    caption=f"🎬 {title} ({choice}p)",
                    supports_streaming=True
                )

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except Exception as e:
        err_str = str(e)
        if "FILE_TOO_LARGE" in err_str or "File is larger than max-filesize" in err_str:
            error_text = (
                "⚠️ Файл весит больше 50 МБ!\n"
                "Telegram ограничивает отправку файлов через ботов до 50 МБ.\n\n"
                "👉 Попробуй нажать на кнопку качества пониже (480p или 360p) или скачай только звук."
            )
        else:
            error_text = f"❌ Ошибка при скачивании:\n{err_str[:180]}"

        try:
            bot.edit_message_text(
                error_text,
                chat_id=chat_id,
                message_id=status_msg.message_id
            )
        except Exception:
            pass

    finally:
        cleanup_files(file_prefix)
        with lock:
            active_downloads.discard(user_id)


if __name__ == '__main__':
    print(f"Бот запущен! FFmpeg установлен: {HAS_FFMPEG}")
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=30)
