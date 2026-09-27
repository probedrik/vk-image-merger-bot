"""
VK бот для объединения изображений.
Полный аналог bot_img2.py, адаптированный под VK Messenger API.

Использует vkbottle — современный асинхронный фреймворк для VK ботов.
"""

import os
import tempfile
import uuid
import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from PIL import Image

from vkbottle import (
    API, Bot, Keyboard, KeyboardButtonColor, Text,
    BaseStateGroup, BuiltinStateDispenser
)
from vkbottle.bot import BotLabeler, Message
from vkbottle.http import AiohttpClient

from your_image_script import combine_images
from yadisk_service import YandexDiskService

# Загрузка переменных окружения
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
VK_TOKEN = os.getenv("VK_TOKEN", "")
YADISK_TOKEN = os.getenv("YADISK_TOKEN")
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ
TEMP_DIR = tempfile.gettempdir()
RESULT_LIFETIME = 3600  # 1 час

# Прокси для исходящих запросов (HTTP_PROXY/HTTPS_PROXY из .env).
# VK API недоступен с зарубежных серверов — трафик идёт через московский squid.
PROXY_URL = (
    os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY") or ""
).strip()


def proxy_label() -> str:
    # Хост:порт прокси для лога — без креденшелов.
    if not PROXY_URL:
        return "без прокси (прямые запросы)"
    try:
        parsed = urlparse(PROXY_URL)
        return f"прокси {parsed.hostname}:{parsed.port}"
    except ValueError:
        return "прокси задан"


# Инициализация сервиса Яндекс.Диска
yadisk_service = YandexDiskService(YADISK_TOKEN) if YADISK_TOKEN else None

# Инициализация бота и хранилища.
# trust_env=True — HTTP-клиент vkbottle берёт прокси из HTTP_PROXY/HTTPS_PROXY (.env),
# иначе VK API недоступен с зарубежных серверов.
bot = Bot(api=API(VK_TOKEN, http_client=AiohttpClient(trust_env=True)))
logger.info(f"Сетевой режим: {proxy_label()}")
labeler = BotLabeler()
bot.labeler = labeler

# Хранилище состояний (FSM)
state_dispenser = BuiltinStateDispenser()

# Хранилище сессий пользователей
user_sessions: dict[int, dict] = {}
results_storage: dict[int, list] = {}


# ---------------------------------------------------------------------------
# Группа состояний (аналог StatesGroup в aiogram)
# ---------------------------------------------------------------------------

class Form(BaseStateGroup):
    WAITING_IMAGES = "waiting_images"
    WAITING_GAP = "waiting_gap"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def get_session(user_id: int) -> dict:
    """Получить или создать сессию пользователя"""
    if user_id not in user_sessions:
        logger.info(f"Creating new session for user_id: {user_id}")
        user_sessions[user_id] = {"temp_files": [], "message_ids": []}
    return user_sessions[user_id]


def is_valid_image(file_path: str, max_size: int = MAX_FILE_SIZE) -> bool:
    """Проверить, что файл — валидное изображение"""
    try:
        if os.path.getsize(file_path) > max_size:
            return False
        with Image.open(file_path) as img:
            img.verify()
        return True
    except Exception:
        return False


async def download_vk_photo(photo_attachment, user_id: int) -> Optional[str]:
    """
    Скачать фото из вложения VK-сообщения.
    Принимает vkbottle PhotosPhoto объект — выбирает наибольшее разрешение.
    """
    photo_url = None

    # 1. Пробуем через sizes (список PhotosPhotoSizes)
    if hasattr(photo_attachment, "sizes") and photo_attachment.sizes:
        best = max(
            photo_attachment.sizes,
            key=lambda s: (getattr(s, "width", 0) or 0) * (getattr(s, "height", 0) or 0),
            default=None
        )
        if best and hasattr(best, "url") and best.url:
            photo_url = best.url

    # 2. Пробуем прямое поле photo_256 (2560px, новое API)
    if not photo_url and hasattr(photo_attachment, "photo_256") and photo_attachment.photo_256:
        photo_url = photo_attachment.photo_256

    # 3. Пробуем поле images (список PhotosImage)
    if not photo_url and hasattr(photo_attachment, "images") and photo_attachment.images:
        best = max(
            photo_attachment.images,
            key=lambda s: (getattr(s, "width", 0) or 0) * (getattr(s, "height", 0) or 0),
            default=None
        )
        if best and hasattr(best, "url") and best.url:
            photo_url = best.url

    if not photo_url:
        logger.warning(f"Не удалось получить URL фото для user_id={user_id}")
        return None

    temp_path = os.path.join(TEMP_DIR, f"{user_id}_{uuid.uuid4().hex[:8]}.jpg")

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(photo_url) as resp:
                if resp.status == 200:
                    with open(temp_path, "wb") as f:
                        f.write(await resp.read())
                else:
                    logger.error(f"Ошибка скачивания фото: HTTP {resp.status}")
                    return None

        if is_valid_image(temp_path):
            return temp_path
        else:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return None

    except Exception as e:
        logger.error(f"Ошибка при скачивании фото VK: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return None


async def send_image_as_photo(
    user_id: int, file_path: str, caption: str = ""
) -> Optional[str]:
    """
    Отправить изображение как фото через photos.getMessagesUploadServer.
    Это надёжнее чем docs-загрузка — VK не сжимает фото при отправке через API.
    Возвращает строку attachment вида 'photo{owner_id}_{photo_id}'.
    """
    try:
        api = bot.api

        # 1. Получаем сервер для загрузки фото
        upload_info = await api.photos.get_messages_upload_server(
            peer_id=user_id
        )
        upload_url = upload_info.upload_url

        # 2. Загружаем файл на сервер
        async with aiohttp.ClientSession(trust_env=True) as session:
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field(
                    "photo", f,
                    filename=os.path.basename(file_path),
                    content_type="image/jpeg"
                )
                async with session.post(upload_url, data=form) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            f"Ошибка загрузки фото: HTTP {resp.status}, body={body[:300]}"
                        )
                        return None
                    upload_result = await resp.json()

        # 3. Проверяем ответ
        if "photo" not in upload_result or "server" not in upload_result:
            logger.error(f"Неожиданный ответ от upload сервера: {upload_result}")
            return None

        # 4. Сохраняем фото
        saved = await api.photos.save_messages_photo(
            photo=upload_result["photo"],
            server=str(upload_result["server"]),
            hash=upload_result.get("hash", "")
        )

        if saved:
            photo_obj = saved[0]
            attachment = f"photo{photo_obj.owner_id}_{photo_obj.id}"

            # 5. Отправляем сообщение с вложением
            await api.messages.send(
                user_id=user_id,
                message=caption or "Готово!",
                attachment=attachment,
                random_id=0
            )
            return attachment

        logger.error("photos.save_messages_photo вернул пустой результат")
        return None

    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        return None


async def send_image_as_document(
    user_id: int, file_path: str, caption: str = ""
) -> Optional[int]:
    """
    Отправить изображение как документ (без сжатия).
    Использует docs.getMessagesUploadServer + docs.save.
    Fallback на send_image_as_photo при ошибке.
    """
    try:
        api = bot.api

        # 1. Получаем сервер для загрузки документа
        upload_info = await api.docs.get_messages_upload_server(
            peer_id=user_id, type="doc"
        )
        upload_url = upload_info.upload_url

        # 2. Загружаем файл
        async with aiohttp.ClientSession(trust_env=True) as session:
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field(
                    "file", f,
                    filename=os.path.basename(file_path),
                    content_type="image/jpeg"
                )
                async with session.post(upload_url, data=form) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            f"docs upload вернул HTTP {resp.status}: {body[:200]}. "
                            f"Пробуем отправить как фото..."
                        )
                        return await send_image_as_photo(user_id, file_path, caption)
                    upload_result = await resp.json()

        if not upload_result or "file" not in upload_result:
            logger.warning(
                f"docs.save не вернул file: {upload_result}. "
                f"Пробуем отправить как фото..."
            )
            return await send_image_as_photo(user_id, file_path, caption)

        # 3. Сохраняем документ
        doc_data = await api.docs.save(
            file=upload_result["file"],
            title=os.path.basename(file_path)
        )

        if doc_data and doc_data.doc:
            owner_id = doc_data.doc.owner_id
            doc_id = doc_data.doc.id
            attachment = f"doc{owner_id}_{doc_id}"

            await api.messages.send(
                user_id=user_id,
                message=caption or "Готово!",
                attachment=attachment,
                random_id=0
            )
            return doc_id

        logger.warning("docs.save вернул пустой результат, пробуем как фото...")
        return await send_image_as_photo(user_id, file_path, caption)

    except Exception as e:
        logger.error(f"Ошибка отправки документа: {e}. Пробуем как фото...")
        return await send_image_as_photo(user_id, file_path, caption)


def build_keyboard(buttons_data: list[list[tuple[str, str, str]]]) -> str:
    """
    Построить VK-клавиатуру.
    buttons_data: [[(label, payload, color), ...], ...]
    color: 'primary', 'secondary', 'negative', 'positive'
    """
    color_map = {
        "primary": KeyboardButtonColor.PRIMARY,
        "secondary": KeyboardButtonColor.SECONDARY,
        "negative": KeyboardButtonColor.NEGATIVE,
        "positive": KeyboardButtonColor.POSITIVE,
    }

    kb = Keyboard(one_time=False, inline=False)

    for i, row in enumerate(buttons_data):
        for label, payload, color in row:
            kb.add(
                Text(label, payload),
                color_map.get(color, KeyboardButtonColor.PRIMARY)
            )
        if i < len(buttons_data) - 1:
            kb.row()

    return kb.get_json()


# ---------------------------------------------------------------------------
# Обработчики сообщений
# ---------------------------------------------------------------------------

@labeler.message(text=["начать", "старт", "меню", "start", "menu", "🔄 Сбросить"])
async def cmd_start(message: Message):
    """Сброс сессии и вывод главного меню"""
    user_id = message.from_id
    # Очистка сессии
    if user_id in user_sessions:
        for f in user_sessions[user_id].get("temp_files", []):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        user_sessions.pop(user_id, None)

    # Создаём новую сессию
    get_session(user_id)

    # Устанавливаем состояние
    await state_dispenser.set(message.from_id, Form.WAITING_IMAGES)

    # Клавиатура
    kb_json = build_keyboard([
        [("📂 Список файлов", '{"cmd":"disk"}', "primary"),
         ("📜 История", '{"cmd":"history"}', "secondary")],
        [("🔄 Сбросить", '{"cmd":"reset"}', "negative")],
    ])

    help_text = (
        "📎 Отправьте 3 изображения или выберите из списка на Яндекс.Диске.\n"
        "Вы также можете отправить номера файлов через пробел (например: 001 045 123).\n\n"
        "Поддерживаемые форматы: JPEG/PNG\n"
        "Максимальный размер файла: 20 МБ\n\n"
        "Команды:\n"
        "  📂 Список файлов — показать файлы Яндекс.Диска\n"
        "  📜 История — показать сохранённые результаты\n"
        "  🔄 Сбросить — сбросить список выбранных файлов"
    )
    await message.answer(help_text, keyboard=kb_json)


@labeler.message(text=["📜 История", "история", "history"])
async def cmd_history(message: Message):
    """Показать историю результатов"""
    user_id = message.from_id
    if user_id not in results_storage or not results_storage[user_id]:
        await message.answer("❌ У вас нет сохранённых результатов.")
        return

    results = results_storage[user_id]
    await message.answer(f"📁 У вас {len(results)} сохранённых результатов:")

    for i, (result_path, _) in enumerate(results, 1):
        if os.path.exists(result_path):
            await send_image_as_document(
                user_id, result_path,
                caption=f"Результат #{i}"
            )
        else:
            await message.answer(f"⚠️ Файл результата #{i} не найден.")


@labeler.message(text=["📂 Список файлов", "диск", "disk", "файлы"])
async def cmd_disk(message: Message):
    """Показать файлы с Яндекс.Диска"""
    if not yadisk_service:
        await message.answer("❌ Интеграция с Яндекс.Диском не настроена.")
        return

    files = await asyncio.to_thread(yadisk_service.list_files)
    if not files:
        await message.answer("📂 Папка 'Принты' пуста или не найдена.")
        return

    # Показываем первые 30 файлов
    shown = files[:30]
    lines = [f"{i+1}. {f['name']}" for i, f in enumerate(shown)]
    text = "📂 Файлы на Яндекс.Диске (Принты):\n\n" + "\n".join(lines)

    if len(files) > 30:
        text += f"\n\n... и ещё {len(files) - 30} файлов."

    text += (
        "\n\n⌨️ Чтобы скачать файлы, отправьте их номера "
        "или имена через пробел.\n"
        "Пример: 1 5 10 — скачает файлы #1, #5, #10"
    )

    await message.answer(text)


@labeler.message(text=["❌ Отмена", "отмена", "cancel"])
async def cmd_cancel(message: Message):
    """Отмена текущей операции"""
    user_id = message.from_id
    # Удаляем temp-файлы перед удалением сессии
    if user_id in user_sessions:
        for f in user_sessions[user_id].get("temp_files", []):
            try:
                if os.path.exists(f):
                    # Не удаляем файлы из кэша Яндекс.Диска
                    if yadisk_service and f.startswith(yadisk_service.cache_dir):
                        continue
                    os.remove(f)
            except Exception:
                pass
        user_sessions.pop(user_id, None)
    await state_dispenser.delete(message.from_id)
    await message.answer("❌ Операция отменена.")
    await cmd_start(message)


# ---------------------------------------------------------------------------
# Обработка вложений (фото/документы) — когда ждём изображения
# ---------------------------------------------------------------------------

@labeler.message()
async def handle_attachments(message: Message):
    """
    Универсальный обработчик: фото + текст в одном сообщении.
    """
    user_id = message.from_id
    current_state = await state_dispenser.get(message.from_id)

    # Если бот ждёт ввода отступа, обрабатываем как число
    if current_state is not None and current_state.state == Form.WAITING_GAP:
        text = message.text.strip() if message.text else ""
        await process_gap_input(message, text)
        return

    # Собираем вложения-фото (vkbottle объекты)
    photo_attachments = []
    if message.attachments:
        for att in message.attachments:
            if att.photo:
                photo_attachments.append(att.photo)

    # Обрабатываем фото
    session = get_session(user_id)
    downloaded_count = 0
    for photo in photo_attachments:
        downloaded = await download_vk_photo(photo, user_id)
        if downloaded:
            session["temp_files"].append(downloaded)
            downloaded_count += 1
        else:
            await message.answer("⚠️ Не удалось загрузить одно из фото.")

    # Если есть фото — отчитываемся
    if photo_attachments:
        current_count = len(session["temp_files"])
        file_names = "\n".join(
            f"- {os.path.basename(f)}" for f in session["temp_files"]
        )
        await message.answer(
            f"📂 Загружено {downloaded_count} из {len(photo_attachments)} фото. "
            f"Всего в сессии: {current_count}\n{file_names}"
        )

        if current_count >= 3:
            await message.answer(
                "🖼 Готово к обработке! Введите отступ между "
                "изображениями в миллиметрах (например, 5):"
            )
            await state_dispenser.set(message.from_id, Form.WAITING_GAP)
        else:
            await message.answer(
                f"📊 Всего файлов: {current_count}. "
                f"Отправьте ещё {3 - current_count}."
            )

    # Обрабатываем текст (может быть в одном сообщении с фото или отдельно)
    text = message.text.strip() if message.text else ""
    if text and text not in (
        "начать", "старт", "меню", "start", "menu",
        "📜 История", "история", "history",
        "📂 Список файлов", "диск", "disk", "файлы",
        "❌ Отмена", "отмена", "cancel",
        "🔄 Сбросить"
    ):
        await handle_text_input(message, text)


# ---------------------------------------------------------------------------
# Обработка текстового ввода (номера/имена файлов Яндекс.Диска)
# ---------------------------------------------------------------------------

async def handle_text_input(message: Message, text: str):
    """Обработка текста как запроса файлов с Яндекс.Диска"""
    if not yadisk_service:
        # Если нет Яндекс.Диска, просто игнорируем текст
        return

    user_id = message.from_id
    args = text.split()

    # Фильтруем: если есть хотя бы одно число или выглядит как имя файла
    has_numbers = any(a.isdigit() for a in args)
    has_filenames = any("." in a for a in args)

    if not (has_numbers or has_filenames):
        return  # Не похоже на запрос файлов

    await process_disk_files(message, args, user_id)


async def process_disk_files(
    message: Message, queries: list[str], user_id: int | None = None
):
    """Скачать файлы с Яндекс.Диска по запросам"""
    if user_id is None:
        user_id = message.from_id

    session = get_session(user_id)
    initial_count = len(session["temp_files"])

    if initial_count > 0:
        initial_names = "\n".join(
            f"- {os.path.basename(f)}" for f in session["temp_files"]
        )
        await message.answer(
            f"📂 Текущие файлы в сессии ({initial_count}):\n"
            f"{initial_names}\n\n"
            f"🔎 Ищу новые файлы: {', '.join(queries)}..."
        )
    else:
        await message.answer(f"🔎 Ищу файлы: {', '.join(queries)}...")

    found_files = yadisk_service.find_files(queries)

    if not found_files:
        await message.answer("❌ Файлы не найдены.")
        return

    file_names = [f["name"] for f in found_files]
    await message.answer(f"📥 Скачиваю файлы: {', '.join(file_names)}")

    downloaded_files = []
    for file_info in found_files:
        file_name = file_info["name"]
        local_path = await asyncio.to_thread(
            yadisk_service.download_file, file_info["path"], file_name)

        if local_path:
            try:
                with Image.open(local_path) as img:
                    img.verify()
                session["temp_files"].append(local_path)
                downloaded_files.append(file_name)
            except Exception:
                if os.path.exists(local_path):
                    os.remove(local_path)
                await message.answer(f"⚠️ Ошибка валидации файла: {file_name}")
        else:
            await message.answer(f"⚠️ Ошибка скачивания: {file_name}")

    current_count = len(session["temp_files"])

    if downloaded_files:
        await message.answer(
            f"✅ Успешно загружено: {', '.join(downloaded_files)}"
        )
        final_names = "\n".join(
            f"- {os.path.basename(f)}" for f in session["temp_files"]
        )
        await message.answer(
            f"📂 Обновлённый список файлов ({current_count}):\n{final_names}"
        )
    else:
        await message.answer("❌ Не удалось загрузить ни одного файла.")

    if current_count >= 3:
        await message.answer(
            "🖼 Готово к обработке! Введите отступ между "
            "изображениями в миллиметрах (например, 5):"
        )
        await state_dispenser.set(message.from_id, Form.WAITING_GAP)
    else:
        await message.answer(
            f"📊 Всего файлов: {current_count}. "
            f"Отправьте ещё {3 - current_count}."
        )
        await state_dispenser.set(message.from_id, Form.WAITING_IMAGES)


# ---------------------------------------------------------------------------
# Обработка ввода отступа
# ---------------------------------------------------------------------------

async def process_gap_input(message: Message, text: str):
    """Обработка ввода отступа между изображениями"""
    user_id = message.from_id
    session = get_session(user_id)

    # Проверяем, что введено число
    if not text.isdigit():
        await message.answer("❌ Введите целое число в миллиметрах (например, 5).")
        return

    gap_mm = int(text)
    files_to_process = session.get("temp_files", [])[:3]

    if len(files_to_process) < 3:
        await message.answer(
            f"❌ Недостаточно файлов. Есть {len(files_to_process)}, нужно 3."
        )
        await state_dispenser.set(message.from_id, Form.WAITING_IMAGES)
        return

    await message.answer(
        f"🖼 Выбран отступ: {gap_mm} мм. Начинаю обработку..."
    )

    # Обработка
    await process_and_send(message, files_to_process, user_id, gap_mm)

    # Удаляем temp-файлы (кроме кэшированных с Яндекс.Диска)
    for f in files_to_process:
        try:
            if os.path.exists(f):
                if yadisk_service and os.path.abspath(f).startswith(
                    os.path.abspath(yadisk_service.cache_dir)
                ):
                    continue
                os.remove(f)
        except Exception:
            pass

    # Сброс
    await state_dispenser.delete(message.from_id)
    session["temp_files"] = []


async def process_and_send(
    message: Message, files: list, user_id: int, gap_mm: int
):
    """Объединить 3 изображения и отправить результат"""
    try:
        await message.answer("⏳ Обрабатываю изображения...")

        logger.info(
            f"Начало обработки VK: user_id={user_id}, "
            f"files={files}, gap={gap_mm}"
        )

        # Проверка существования файлов
        for f in files:
            if not os.path.exists(f):
                logger.error(f"Файл не найден: {f}")
                await message.answer(
                    f"❌ Ошибка: файл не найден {os.path.basename(f)}"
                )
                return

        output_path = os.path.join(
            TEMP_DIR, f"vk_result_{uuid.uuid4().hex[:8]}.jpg"
        )

        # Запуск в отдельном потоке
        success = await asyncio.to_thread(
            combine_images,
            image_paths=files,
            output_filename=output_path,
            page_width_mm=210,
            gap_mm=gap_mm,
            max_dpi=450,
            quality=95,
        )

        logger.info(f"Результат combine_images: {success}")

        if success and os.path.exists(output_path):
            # Сохраняем в историю
            if user_id not in results_storage:
                results_storage[user_id] = []
            current_time = asyncio.get_event_loop().time()
            results_storage[user_id].append((output_path, current_time))

            await message.answer(
                f"✅ Обработка завершена! Отступ: {gap_mm} мм"
            )

            # Отправляем результат как документ (без пережатия VK)
            result_id = await send_image_as_document(
                user_id, output_path,
                caption=f"✨ Объединённое изображение (отступ {gap_mm} мм)"
            )

            if not result_id:
                await message.answer(
                    "❌ Не удалось отправить результат. Попробуйте снова."
                )
        else:
            await message.answer("❌ Ошибка при обработке файлов.")

    except Exception as e:
        logger.exception(f"Ошибка в process_and_send: {e}")
        await message.answer("⛔ Внутренняя ошибка. Попробуйте позже.")


# ---------------------------------------------------------------------------
# Периодическая очистка старых результатов
# ---------------------------------------------------------------------------

async def cleanup_old_results():
    """Фоновая задача: удаление результатов старше RESULT_LIFETIME"""
    while True:
        current_time = asyncio.get_event_loop().time()
        to_delete = []

        for user_id, results in list(results_storage.items()):
            expired_files = []
            for result_path, timestamp in results[:]:
                if current_time - timestamp > RESULT_LIFETIME:
                    try:
                        if os.path.exists(result_path):
                            os.remove(result_path)
                        expired_files.append((result_path, timestamp))
                    except Exception as e:
                        logger.error(f"Ошибка удаления {result_path}: {e}")

            results_storage[user_id] = [
                r for r in results if r not in expired_files
            ]
            if not results_storage[user_id]:
                to_delete.append(user_id)

        for user_id in to_delete:
            del results_storage[user_id]

        await asyncio.sleep(300)  # Каждые 5 минут


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not VK_TOKEN:
        logger.error(
            "❌ VK_TOKEN не задан! Добавьте его в .env файл:\n"
            "   VK_TOKEN=ваш_токен_группы_vk"
        )
        logger.info(
            "   Инструкция:\n"
            "   1. Создайте группу ВКонтакте\n"
            "   2. Управление → Работа с API → Создать ключ\n"
            "   3. Разрешите доступ к сообщениям сообщества\n"
            "   4. Включите Long Poll API: Управление → API → Long Poll → Включено\n"
            "   5. Укажите версию API: 5.199\n"
            "   6. Скопируйте токен в .env"
        )
        exit(1)

    # Добавляем фоновую задачу очистки и запускаем бота
    bot.loop_wrapper.add_task(cleanup_old_results)
    bot.run_forever()
