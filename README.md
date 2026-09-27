# VK Image Merger Bot

Асинхронный бот ВКонтакте для объединения изображений в вертикальный коллаж.

## Возможности

- Принимает 3 изображения (из вложений или с Яндекс.Диска)
- Объединяет вертикально в один файл (A4, настраиваемый отступ)
- Отправляет результат как документ без сжатия

## Быстрый старт

### Локально

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Создайте `.env`:
```env
VK_TOKEN=ваш_токен_группы
YADISK_TOKEN=ваш_токен_яндекс_диска
```

На зарубежных серверах VK API недоступен напрямую — добавьте прокси (весь
исходящий трафик пойдёт через него: VK API, загрузка/скачивание файлов,
Яндекс.Диск):
```env
HTTP_PROXY=http://user:pass@host:port
HTTPS_PROXY=http://user:pass@host:port
NO_PROXY=localhost,127.0.0.1
```

Запуск:
```bash
python vk_bot.py
```

### Docker

```bash
docker build -t vk-image-bot .
docker run -d --name vk-image-bot --env-file .env --restart unless-stopped vk-image-bot
```

## Лицензия

MIT
