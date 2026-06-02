from PIL import Image
import os
import logging

logger = logging.getLogger(__name__)


def combine_images(
    image_paths: list,
    output_filename: str,
    page_width_mm: float = 210,  # Ширина в миллиметрах (A4)
    gap_mm: float = 5,
    max_dpi: int = 300,
    quality: int = 90
):
    """Объединяет изображения вертикально с контролем размеров"""
    try:
        logger.info(f"Начало combine_images. Файлов: {len(image_paths)}")
        # Конвертация физических размеров в пиксели
        dpi = min(max_dpi, 300)  # Ограничиваем DPI
        px_per_mm = dpi / 25.4
        target_width = int(page_width_mm * px_per_mm)
        gap_px = int(gap_mm * px_per_mm)

        # Проверка ограничений Pillow
        MAX_DIMENSION = 65500
        if target_width > MAX_DIMENSION:
            raise ValueError(
                f"Ширина превышает {MAX_DIMENSION} пикселей при DPI={dpi}")

        # Загрузка и масштабирование
        images = []
        total_height = 0
        for i, path in enumerate(image_paths):
            logger.info(f"Обработка файла {i+1}/{len(image_paths)}: {path}")
            if not os.path.exists(path):
                raise FileNotFoundError(f"Файл {path} не найден")

            with Image.open(path) as img:
                img.verify()
                img = Image.open(path)  # Переоткрыть после verify()

                # Масштабирование с сохранением пропорций
                scaling_factor = target_width / img.width
                new_height = int(img.height * scaling_factor)

                # Проверка высоты
                if (total_height + new_height + gap_px) > MAX_DIMENSION:
                    raise ValueError(
                        "Суммарная высота превышает 65500 пикселей")

                logger.info(f"Ресайз изображения {i+1}...")
                # Используем BICUBIC вместо LANCZOS для ускорения
                img_resized = img.resize(
                    (target_width, new_height), Image.BOX)
                images.append(img_resized)
                total_height += new_height + gap_px

        total_height -= gap_px  # Убираем последний отступ

        # Создание холста
        if output_filename.lower().endswith(('.jpg', '.jpeg')):
            mode = 'RGB'
        else:
            mode = 'RGBA' if any(
                img.mode == 'RGBA' for img in images) else 'RGB'

        result = Image.new(mode, (target_width, total_height), (255, 255, 255))

        # Склейка изображений
        y_offset = 0
        for img in images:
            if mode == 'RGB' and img.mode != 'RGB':
                img = img.convert('RGB')
            result.paste(img, (0, y_offset))
            y_offset += img.height + gap_px

        # Сохранение с параметрами
        save_args = {
            'dpi': (dpi, dpi),
            'quality': quality,
            'optimize': False  # Отключаем оптимизацию для ускорения сохранения
        } if output_filename.lower().endswith(('.jpg', '.jpeg')) else {
            'dpi': (dpi, dpi)
        }

        logger.info("Сохранение результата...")
        result.save(output_filename, **save_args)
        logger.info(f"Файл {output_filename} успешно создан!")
        return True

    except Exception as e:
        logger.error(f"Ошибка в combine_images: {str(e)}")
        return False


# Пример использования
if __name__ == "__main__":
    combine_images(
        image_paths=['image1.jpg', 'image2.jpg', 'image3.jpg'],
        output_filename='result.jpg',
        page_width_mm=210,  # Ширина как у листа A4
        gap_mm=5,
        max_dpi=350  # Снижаем DPI для уменьшения размера
    )
