import os
import requests
import logging
from typing import List, Optional, Dict, Union
from urllib.parse import quote

logger = logging.getLogger(__name__)


class YandexDiskService:
    def __init__(self, token: str, cache_dir: str = "downloads"):
        self.base_url = "https://cloud-api.yandex.net/v1/disk"
        self.headers = {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json"
        }
        self.cache_dir = cache_dir
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Выполнение запроса к API"""
        try:
            url = f"{self.base_url}{endpoint}"
            response = requests.request(
                method, url, headers=self.headers, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса к Яндекс.Диску: {e}")
            return None

    def list_files(self, path: str = "Принты", limit: int = 1000) -> List[Dict]:
        """Получение списка файлов в директории"""
        params = {
            "path": path,
            "limit": limit,
            "media_type": "image",
            "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.mime_type,_embedded.items.size"
        }

        data = self._make_request("GET", "/resources", params)
        if not data or "_embedded" not in data:
            return []

        return data["_embedded"]["items"]

    def find_files(self, queries: List[str], path: str = "Принты") -> List[Dict]:
        """
        Поиск файлов по списку запросов (имена или номера).
        Возвращает список найденных файлов в порядке запросов.
        """
        all_files = self.list_files(path)
        if not all_files:
            return []

        found_files = []

        for query in queries:
            match = None

            # Попытка найти точное совпадение по имени
            for file in all_files:
                if file["name"] == query:
                    match = file
                    break

            # Если не найдено, ищем по номеру (если query - число)
            if not match and query.isdigit():
                # Нормализация номера до 4 цифр (например, "1" -> "0001")
                # Ищем вхождение номера в имя файла
                formatted_num = f"{int(query):04d}"
                for file in all_files:
                    if formatted_num in file["name"]:
                        match = file
                        break

            # Если все еще не найдено, пробуем частичное совпадение имени
            if not match:
                for file in all_files:
                    if query.lower() in file["name"].lower():
                        match = file
                        break

            if match:
                found_files.append(match)
            else:
                logger.warning(f"Файл по запросу '{query}' не найден")

        return found_files

    def get_cached_file(self, file_name: str) -> Optional[str]:
        """Проверка наличия файла в кэше"""
        cached_path = os.path.join(self.cache_dir, file_name)
        if os.path.exists(cached_path):
            return cached_path
        return None

    def download_file(self, file_path: str, file_name: str) -> Optional[str]:
        """
        Скачивание файла по пути на диске.
        Возвращает путь к локальному файлу (из кэша или скачанный).
        """
        # Проверяем кэш
        cached_path = self.get_cached_file(file_name)
        if cached_path:
            logger.info(f"Файл найден в кэше: {file_name}")
            return cached_path

        save_path = os.path.join(self.cache_dir, file_name)

        try:
            params = {"path": file_path}
            data = self._make_request("GET", "/resources/download", params)

            if not data or "href" not in data:
                return None

            download_url = data["href"]

            logger.info(f"Скачивание файла: {file_name}")
            with requests.get(download_url, stream=True) as r:
                r.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return save_path

        except Exception as e:
            logger.error(f"Ошибка при скачивании файла {file_path}: {e}")
            if os.path.exists(save_path):
                os.remove(save_path)
            return None
