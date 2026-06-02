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
