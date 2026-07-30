import os
import json
import logging
import sys
import time
import io
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.errors import HttpError
from chat_converter import ChatParser
import threading
import random
import socket

socket.setdefaulttimeout(20)
SCOPES = ['https://www.googleapis.com/auth/drive']
CREDENTIALS_FILE = 'data/credentials.json'
TOKEN_FILE = 'data/token.json'
CONFIG_FILE = 'data/config.json'
max_workers = min(8, os.cpu_count())  # Макс. кол-во параллельных процессов

os.makedirs("data", exist_ok=True)

file_handler = logging.FileHandler("data/app.log", mode='a', encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] -> %(message)s'))
file_handler.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(message)s'))  # Чистый вывод на экран
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[file_handler, console_handler]
)


class GoogleDriveAdapter:
    def __init__(self):
        self.creds = None
        self._thread_local = threading.local()
        self.authenticate()

    @property
    def service(self):
        # Гарантирует отдельный потокобезопасный SSL-сокет для каждого параллельного потока
        if not hasattr(self._thread_local, 'service_instance'):
            self._thread_local.service_instance = build('drive', 'v3', credentials=self.creds, cache_discovery=False)
        return self._thread_local.service_instance

    def init_infrastructure(self):
        logging.info("Поиск инфраструктуры Google AI Studio")

        studio_folder = self.find_folder("Google AI Studio", parent_id="root")
        if not studio_folder:
            raise Exception("Папка 'Google AI Studio' не найдена в корне")

        studio_id = studio_folder['id']
        logging.info(f"Найдена корневая папка 'Google AI Studio' (ID: {studio_id})")

        chats_folder = self.find_folder("Chats", parent_id=studio_id)
        if not chats_folder:
            logging.info("Папка 'Chats' не найдена. Создаю...")
            chats_folder = self.create_folder("Chats", studio_id)
        trash_folder = self.find_folder("_Trash", parent_id=studio_id)
        if not trash_folder:
            logging.info("Папка '_Trash' не найдена. Создаю...")
            trash_folder = self.create_folder("_Trash", studio_id)
        map_file = self.find_file("_map.json", parent_id=studio_id)

        return {
            "studio_id": studio_id,
            "chats_id": chats_folder['id'],
            "trash_id": trash_folder['id'],
            "map_id": map_file['id'] if map_file else None
        }

    def authenticate(self, force_select=False):
        if os.path.exists(TOKEN_FILE) and not force_select:
            self.creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token and not force_select:
                try:
                    self.creds.refresh(Request())
                except Exception:
                    logging.error("Ошибка обновления токена: ", exc_info=True)
                    self.creds = None

            if not self.creds:
                if not os.path.exists(CREDENTIALS_FILE):
                    raise FileNotFoundError(f"Файл {CREDENTIALS_FILE} не найден")

                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                self.creds = flow.run_local_server(port=0, prompt='select_account')

            with open(TOKEN_FILE, 'w') as token:
                token.write(self.creds.to_json())

    def get_current_user_email(self):
        try:
            request = self.service.about().get(fields='user')
            about = self.execute_with_retry(request)
            return about.get('user', {}).get('emailAddress', 'Неизвестно')
        except Exception:
            return 'Неизвестно'

    def switch_account(self):
        # Удаляет старый токен и запускает принудительную повторную авторизацию
        logging.info("Выход из текущего аккаунта Google...")
        if os.path.exists(TOKEN_FILE):
            try:
                os.remove(TOKEN_FILE)
            except Exception as e:
                logging.error(f"Не удалось удалить файл токена: {e}")
        self.creds = None
        if hasattr(self._thread_local, 'service_instance'):
            del self._thread_local.service_instance
        # Запускаем повторный вход с принудительным выбором аккаунта
        self.authenticate(force_select=True)

    def execute_with_retry(self, request, max_attempts=4):
        for attempt in range(max_attempts):
            try:
                return request.execute()
            except Exception as e:
                # Если файл не найден (404), повторные попытки бессмысленны - выбрасываем сразу
                if isinstance(e, HttpError):
                    if e.resp.status == 404 or "fileNotDownloadable" in str(e):
                        return
                if attempt == max_attempts - 1:
                    logging.error(f"ОШИБКА API: {e}")
                    logging.error(f"Запрос {request} не будет выполнен!!!")
                    raise e
                sleep_time = 2 ** attempt + random.uniform(0, 2)
                logging.warning(f"Ошибка API: {e}. Повтор через {sleep_time:.2f} сек...")
                time.sleep(sleep_time)

    def create_folder(self, name, parent_id):
        metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        request = self.service.files().create(body=metadata, fields='id')
        return self.execute_with_retry(request)

    def get_file_info(self, file_id):
        request = self.service.files().get(fileId=file_id, fields='id, name, trashed, parents')
        try:
            return self.execute_with_retry(request)
        except Exception:
            return None

    def update_item(self, item_id, new_parent_id=None, new_name=None):
        # Атомарно перемещает и/или переименовывает объект за 1 HTTP-запрос!
        if new_parent_id and str(new_parent_id).startswith("temp_"):
            raise ValueError(f"Критическая ошибка: недопустимый ID целевой папки '{new_parent_id}'")
        body = {}
        if new_name:
            body['name'] = new_name
        params = {'fileId': item_id, 'fields': 'id, name, parents'}
        if body:
            params['body'] = body
        if new_parent_id:
            request = self.service.files().get(fileId=item_id, fields='parents')
            file_info = self.execute_with_retry(request) or {}
            current_parents = ",".join(file_info.get('parents', []))
            params['addParents'] = new_parent_id
            params['removeParents'] = current_parents

        request = self.service.files().update(**params)
        return self.execute_with_retry(request)

    def upload_file(self, name, parent_id, content_str, mime_type='text/plain', file_id=None):
        media = MediaIoBaseUpload(io.BytesIO(content_str.encode('utf-8')), mimetype=mime_type, resumable=True)
        if file_id:  # Обновляем существующий файл
            metadata = {'name': name}
            request = self.service.files().update(fileId=file_id, body=metadata, media_body=media, fields='id')
        else:  # Создаем новый файл
            metadata = {'name': name, 'parents': [parent_id]}
            request = self.service.files().create(body=metadata, media_body=media, fields='id')
        return self.execute_with_retry(request)

    def find_item(self, name, parent_id=None, is_folder=None):
        safe_name = name.replace("'", "\\'").replace("\\", "\\\\")
        query = f"name='{safe_name}' and trashed=false"
        if is_folder is not None:
            operator = "=" if is_folder else "!="
            query += f" and mimeType {operator} 'application/vnd.google-apps.folder'"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        request = self.service.files().list(q=query, spaces='drive', fields='files(id, name)')
        results = self.execute_with_retry(request)
        items = results.get('files', [])
        if len(items) > 1:
            drive_link = f"https://drive.google.com/open?id={items[0]['id']}"
            logging.warning(
                f"Найдено несколько объектов с именем '{name}' на Google Диске!"
                f"Выбран объект с ID: {items[0]['id']}. Ссылка: {drive_link}")
        return items[0] if items else None

    def find_folder(self, name, parent_id=None):
        return self.find_item(name, parent_id, is_folder=True)

    def find_file(self, name, parent_id=None):
        return self.find_item(name, parent_id, is_folder=False)

    def get_all_descendants(self, root_id, exclude_ids=None):
        # рекурсивно получает все файлы и папки внутри root_id
        if exclude_ids is None:
            exclude_ids = set()
        all_items = []
        scanned_folders = set()
        folders_to_scan = {root_id}

        def scan_folder(folder_id):
            query = f"'{folder_id}' in parents and trashed=false"
            page_token = None
            local_items = []
            local_subfolders = []
            while True:
                request = self.service.files().list(
                    q=query, spaces='drive',
                    fields='nextPageToken, files(id, name, mimeType, modifiedTime, parents)',
                    pageToken=page_token
                )
                results = self.execute_with_retry(request)
                items = results.get('files', [])
                local_items.extend(items)

                for item in items:
                    if item['mimeType'] == 'application/vnd.google-apps.folder':
                        local_subfolders.append(item['id'])

                page_token = results.get('nextPageToken', None)
                if not page_token:
                    break
            return local_items, local_subfolders

        # Сканируем иерархию папок параллельными волнами (слоями)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while folders_to_scan:
                current_batch = [f for f in folders_to_scan if f not in scanned_folders and f not in exclude_ids]
                if not current_batch:
                    break
                scanned_folders.update(current_batch)
                folders_to_scan = set()
                futures = [executor.submit(scan_folder, f_id) for f_id in current_batch]
                for future in as_completed(futures):
                    try:
                        items, subfolders = future.result()
                        all_items.extend(items)
                        for sf in subfolders:
                            if sf not in scanned_folders:
                                folders_to_scan.add(sf)
                    except Exception:
                        logging.error("Ошибка при параллельном сканировании папки: ", exc_info=True)

        return all_items

    def download_json(self, file_id):
        # Скачивает файл с Google Диска напрямую в память и парсит JSON
        try:
            request = self.service.files().get_media(fileId=file_id)
            content = self.execute_with_retry(request)
            return json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError, HttpError):
            # Файл не является скачиваемым JSON (Google Doc, медиа или бинарный файл) — тихий пропуск
            return None
        except Exception:
            logging.error(f"Ошибка скачивания или чтения JSON с ID={file_id}: ", exc_info=True)
            return None


class AppLogic:
    def __init__(self):
        logging.info("Программа запущена")
        self.sys_folders = None
        self.api = GoogleDriveAdapter()

    @staticmethod
    def prompt_choice(question, clue_text, valid_options, error_template="Вы ввели `{choice}`, попробуйте ещё раз"):
        # Вспомогательный метод: запрашивает ввод у пользователя, пока тот не введет допустимый вариант
        while True:
            print(question)
            choice = input(clue_text).strip()
            if choice in valid_options:
                return choice
            print(error_template.format(choice=choice))

    def run_mode_1(self, thoughts_needed, auto_confirm):
        self.sys_folders = self.api.init_infrastructure()
        if thoughts_needed is None:
            print()
            choice = self.prompt_choice("Нужно ли сохранять в заметку thoughts моделей?",
                                        "Введите 1, чтобы сохранять, или 2, чтобы не сохранять: ", {"1", "2"})
            print()
            thoughts_needed = (choice == "1")
        print()
        self.phase_1_analyze(thoughts_needed=thoughts_needed)
        print()
        # Если авто-подтверждение выключено, запрашиваем подтверждение
        should_execute = auto_confirm
        if not should_execute:
            choice = self.prompt_choice("Начать выполнение плана?",
                                        "Введите 1, чтобы начать, и 2, чтобы отказаться: ", {"1", "2"})
            should_execute = (choice == "1")
        if should_execute:
            print()
            self.phase_2_execute()
            print()

    def run_mode_2(self):
        # Единая точка запуска Режима 2 (откат)
        self.sys_folders = self.api.init_infrastructure()
        self.rollback_to_root()

    def run(self):
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                logging.info(f"Найден файл авто-настроек {CONFIG_FILE}. Автоматический запуск...")
            except Exception:
                logging.error(f"Ошибка чтения файла `{CONFIG_FILE}`:", exc_info=True)
        try:
            while True:
                current_email = self.api.get_current_user_email()
                print(f"Текущий аккаунт Google: `{current_email}`")
                mode = str(cfg.get("mode", "")).strip() if cfg else ""
                if mode not in {'0', '1', '2', '3'}:
                    print("\n ВЫБЕРИТЕ РЕЖИМ:")
                    print(" 0 - сменить гугл-аккаунт")
                    print(" 1 - Навести порядок: рассортировать чаты по папкам и создать заметки")
                    print(" 2 - Откат: вытряхнуть все файлы в папку `Google AI Studio`")
                    print(" 3 - Завершить программу")
                    mode = self.prompt_choice('', "Введите 0, 1, 2 или 3: ", {"0", "1", "2", "3"})
                if mode == "0":
                    self.api.switch_account()
                    new_email = self.api.get_current_user_email()
                    print(f"\nУспешно! Новый аккаунт: `{new_email}`")
                elif mode == "1":
                    raw_thoughts = cfg.get("include_thoughts")
                    self.run_mode_1(
                        thoughts_needed=raw_thoughts in {'1', True} if raw_thoughts is not None else None,
                        auto_confirm=cfg.get("auto_confirm") in {'1', True}
                    )
                elif mode == "2":
                    self.run_mode_2()
                else:
                    logging.info("Программа завершена")
                    break
                if cfg.get("mode") in {"0", "1", "2"}:
                    logging.info("Программа завершена")
                    break
                input("\nНажмите Enter, чтобы продолжить")
        except Exception:
            logging.error("Критическая ошибка: ", exc_info=True)

    @staticmethod
    def get_unique_folder_name(title, allocated_names):
        # Очищает заголовок и гарантирует уникальность: Title, Title (1), Title (2)...
        invalid_chars = set('<>:"/\\|?*')
        clean_title = "".join(c for c in title if c not in invalid_chars and ord(c) >= 32).rstrip(' .')
        if not clean_title:
            clean_title = "Undefined"
        candidate = clean_title
        counter = 1
        while candidate.lower() in allocated_names:
            candidate = f"{clean_title} ({counter})"
            counter += 1
        allocated_names.add(candidate.lower())
        return candidate

    def ensure_virtual_folder(self, vfs, folder_name, parent_id, temp_id):
        # Проверяет наличие папки в VFS или добавляет виртуальную папку в план
        for v_id, v_item in vfs.items():
            if v_item['mimeType'] == 'application/vnd.google-apps.folder':
                if v_item['name'] == folder_name and v_item.get('parents', [None])[0] == parent_id:
                    return v_id
        self.transaction_plan["create_folders"].append({
            "temp_id": temp_id,
            "name": folder_name,
            "parent_id": parent_id
        })
        vfs[temp_id] = {
            "id": temp_id,
            "name": folder_name,
            "parents": [parent_id],
            "mimeType": "application/vnd.google-apps.folder"
        }
        return temp_id

    def resolve_chat_folder(self, vfs, item_id, item_name, allocated_chat_names):
        # Определяет папку для чата и обрабатывает переименование в Студии
        current_parent = vfs[item_id].get('parents', [None])[0]
        parent_item = vfs.get(current_parent, {})
        chats_id = self.sys_folders.get('chats_id')
        is_already_in_chats = chats_id and parent_item.get('parents', [None])[0] == self.sys_folders['chats_id']
        if is_already_in_chats:
            target_chat_folder_id = current_parent
            old_folder_name = parent_item['name']
            # Если чат переименовали в Студии - обновляем название папки
            if item_name != old_folder_name:
                safe_title = self.get_unique_folder_name(item_name, allocated_chat_names)
                self.transaction_plan["move_items"].append({
                    "item_id": target_chat_folder_id,
                    "new_name": safe_title
                })
                vfs[target_chat_folder_id]['name'] = safe_title
            else:
                safe_title = old_folder_name
        else:
            # Новый чат или чат из другой папки - создаем новую папку
            safe_title = self.get_unique_folder_name(item_name, allocated_chat_names)
            temp_folder_id = f"temp_folder_{item_id}"
            target_chat_folder_id = temp_folder_id

            self.transaction_plan["create_folders"].append({
                "temp_id": temp_folder_id,
                "name": safe_title,
                "parent_id": self.sys_folders['chats_id']
            })

            vfs[temp_folder_id] = {
                "id": temp_folder_id,
                "name": safe_title,
                "parents": [self.sys_folders['chats_id']],
                "mimeType": "application/vnd.google-apps.folder"
            }

        return target_chat_folder_id, safe_title

    def simulate_attachments(self, vfs, target_chat_folder_id, attachments, protected_file_ids, claimed_attachments,
                             drive_att_info=None):
        # Обрабатывает вложения (переносит локальные и фиксирует сторонние с Google Диска)
        if drive_att_info is None:
            drive_att_info = {}
        resolved_files = {}
        for att_id in attachments:
            if att_id in vfs:
                att_info = vfs[att_id]
            else:
                att_info = drive_att_info.get(att_id)
                if att_info == "NOT_FOUND":
                    att_info = None
            if att_info and not att_info.get("trashed"):
                orig_name = att_info["name"]
                if att_id in vfs:
                    # Локальный файл из Студии - перемещаем в Attachments/ext
                    ext = orig_name.split('.')[-1].lower() if "." in orig_name else "unknown"
                    # Если файл ЕЩЕ НЕ БЫЛ затребован ни одним чатом в этой сессии
                    if att_id not in claimed_attachments:
                        att_base_id = self.ensure_virtual_folder(
                            vfs, "Attachments", target_chat_folder_id, f"temp_att_base_{target_chat_folder_id}"
                        )
                        ext_folder_id = self.ensure_virtual_folder(
                            vfs, ext, att_base_id, f"temp_att_{ext}_{target_chat_folder_id}"
                        )
                        att_parent = att_info.get('parents', [None])[0]
                        if att_parent != ext_folder_id:
                            self.transaction_plan["move_items"].append({
                                "item_id": att_id,
                                "new_parent_id": ext_folder_id
                            })
                            vfs[att_id]['parents'] = [ext_folder_id]
                        claimed_attachments.add(att_id)

                    resolved_files[att_id] = {"name": orig_name, "status": "active_local", "ext": ext}
                    protected_file_ids.add(att_id)
                else:
                    # Файл с личного Google Диска - не перемещаем!
                    resolved_files[att_id] = {"name": orig_name, "status": "active_drive"}
            else:
                resolved_files[att_id] = {"status": "deleted_by_user"}

        return resolved_files

    def run_mark_and_sweep(self, vfs, protected_file_ids, system_names):
        # Сборка мусора по виртуальному дереву VFS
        logging.info("Расчет сборщика мусора по виртуальному дереву...")
        protected_folder_ids = {self.sys_folders['studio_id'], self.sys_folders['chats_id'],
                                self.sys_folders['trash_id']}
        # Поднимаемся вверх от каждого защищенного файла
        for f_id in protected_file_ids:
            curr_id = f_id
            while curr_id in vfs and curr_id is not None:
                p_id = vfs[curr_id].get('parents', [None])[0]
                if p_id:
                    protected_folder_ids.add(p_id)
                curr_id = p_id

        # Собираем мусорные папки
        useless_folder_ids = set()
        for item_id, item in vfs.items():
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                if item_id not in protected_folder_ids:
                    useless_folder_ids.add(item_id)
        # Добавляем в план только верхнеуровневые мусорные объекты
        for item_id, item in vfs.items():
            if item_id.startswith("temp_"):
                continue
            parent_id = item.get('parents', [None])[0]
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                if item_id in useless_folder_ids and parent_id not in useless_folder_ids:
                    self.transaction_plan["trash_items"].append({
                        "item_id": item_id,
                        "name": item['name'],
                        "old_parent_id": parent_id
                    })
            else:
                if item_id not in protected_file_ids and item['name'] not in system_names:
                    if parent_id not in useless_folder_ids:
                        self.transaction_plan["trash_items"].append({
                            "item_id": item_id,
                            "name": item['name'],
                            "old_parent_id": parent_id
                        })

    def rollback_to_root(self):
        logging.info("СТАРТ ОТКАТА: Вытряхивание всех файлов в корень Google AI Studio...")
        studio_id = self.sys_folders['studio_id']
        trash_id = self.sys_folders['trash_id']
        map_id = self.sys_folders['map_id']

        # 1. Читаем карту _map.json, чтобы узнать ID всех созданных заметок
        generated_note_ids = set()
        if map_id:
            downloaded_map = self.api.download_json(map_id)
            if isinstance(downloaded_map, dict):
                for info in downloaded_map.values():
                    if isinstance(info, dict) and info.get("note_id"):
                        generated_note_ids.add(info["note_id"])

        all_items = self.api.get_all_descendants(studio_id, exclude_ids={trash_id})
        # 1. Извлекаем ВСЕ файлы из любых подпапок обратно в корень
        # 2. Разделяем файлы на оригинальные (извлечь в корень) и искуственно созданные заметки (в _Trash)
        files_to_trash = generated_note_ids
        if map_id: files_to_trash.append(map_id)
        files_to_extract = [item['id'] for item in all_items
            if item['mimeType'] != 'application/vnd.google-apps.folder'
                and item['id'] not in generated_note_ids
                and studio_id not in item.get('parents', [])
        ]
        # 3. Верхнеуровневые папки (например, Chats) -> отправляем в _Trash
        top_folders_to_trash = [
            item for item in all_items
            if item['mimeType'] == 'application/vnd.google-apps.folder'
               and studio_id in item.get('parents', [])
               and item['id'] != trash_id
        ]
        logging.info(f"Найдено файлов для перемещения в корень: {len(files_to_extract)}")
        logging.info(f"Найдено созданных скриптом заметок для перемещения в корень: {len(files_to_trash)}")

        def extract_file(item):
            old_parent = item.get('parents', [None])[0]
            if old_parent:
                self.api.update_item(item_id=item['id'], new_parent_id=studio_id)

        if files_to_extract:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(extract_file, f) for f in files_to_extract]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        logging.error("Ошибка при вытряхивании файла в корень: ", exc_info=True)
        # 3. Отправляем верхнеуровневые папки целиком в _Trash
        logging.info("Перемещение верхнеуровневых папок в _Trash...")
        for folder in top_folders_to_trash:
            try:
                self.api.update_item(folder['id'], trash_id)
                logging.info(f"Папка '{folder['name']}' ушла в _Trash со всеми подпапками")
            except Exception:
                logging.error(f"Ошибка при переносе папки '{folder['name']}' в _Trash", exc_info=True)
        logging.info("ОТКАТ ЗАВЕРШЕН! Все файлы в корне, а папки удалены.")

    def phase_1_analyze(self, thoughts_needed):
        logging.info("ФАЗА 1: Анализ облака и виртуальная симуляция (VFS)")
        # 0. Загрузка древа файлов
        logging.info("Рекурсивное сканирование Google AI Studio...")
        all_items = self.api.get_all_descendants(self.sys_folders['studio_id'])
        vfs = {item['id']: dict(item) for item in all_items}
        self.map_data = {}
        if self.sys_folders['map_id']:
            downloaded_map = self.api.download_json(self.sys_folders['map_id'])
            if isinstance(downloaded_map, dict):
                self.map_data = {c_id: info for c_id, info in downloaded_map.items() if c_id in vfs}
                logging.info("Файл `_map.json` успешно загружен")
            else:
                logging.warning("Файл `_map.json` поврежден. Карта будет создана заново")
        self.transaction_plan = {
            "create_folders": [],
            "move_items": [],
            "upload_files": [],
            "trash_items": [],
            "chats_to_map": []
        }
        system_names = {"Chats", "_Trash", "_map.json", "_RoadMap.md"}
        protected_file_ids = set()
        claimed_attachments = set()
        # Защищаем системные файлы
        for item_id, item in vfs.items():
            if (item['name'] in {"_map.json", "_RoadMap.md"}
                    and item.get('parents', [None])[0] == self.sys_folders['studio_id']):
                protected_file_ids.add(item_id)
        # Запоминаем имена папок в Chats/
        allocated_chat_names = {
            item['name'].lower() for item_id, item in vfs.items()
            if item['mimeType'] == 'application/vnd.google-apps.folder' and item.get('parents', [None])[0] ==
               self.sys_folders['chats_id']
        }
        # 2. Анализ чатов
        logging.info("Анализ чатов")
        json_chats = [
            (item_id, item) for item_id, item in vfs.items()
            if item.get('mimeType') == 'application/vnd.google-makersuite.prompt'
        ]
        # 1. Параллельное скачивание всех JSON-файлов чатов по сети
        logging.info("Анализ чатов: Параллельное скачивание данных...")
        downloaded_jsons = {}

        def _fetch_json(f_id):
            return f_id, self.api.download_json(f_id)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_fetch_json, f_id) for f_id, msg in json_chats]
            for future in as_completed(futures):
                f_id, data = future.result()
                if data:
                    downloaded_jsons[f_id] = data
        # 2. Сбор внешних аттачментов и параллельный запрос их инфо
        external_att_ids = set()
        for f_id, j_data in downloaded_jsons.items():
            for att_id in ChatParser.extract_attachment_ids(j_data):
                if att_id not in vfs:
                    external_att_ids.add(att_id)
        drive_att_cache = {}
        if external_att_ids:
            logging.info("Анализ чатов: Параллельный запрос внешних вложений...")

            def _fetch_att(a_id):
                return a_id, self.api.get_file_info(a_id)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_fetch_att, aid) for aid in external_att_ids]
                for future in as_completed(futures):
                    aid, info = future.result()
                    drive_att_cache[aid] = info if info else "NOT_FOUND"
        # 3. Последовательное мгновенное построение VFS в ОЗУ
        logging.info("Генерация транзакционного плана...")
        for item_id, item in json_chats:
            item_name = item['name']
            json_data = downloaded_jsons.get(item_id)
            map_info = self.map_data.get(item_id, {})
            if isinstance(self.map_data.get(item_id), dict) and map_info.get("note_id") in vfs:
                existing_md_id = map_info.get("note_id")
            else:
                existing_md_id = None
            # Добавляем в карту чат, время последнего изменения и id заметки
            self.transaction_plan["chats_to_map"].append({
                "chat_id": item_id,
                "modifiedTime": item['modifiedTime'],
                "note_id": existing_md_id,
                "thoughts_needed": thoughts_needed
            })
            # Защищаем от удаления все аттачменты и сам чат
            attachments = ChatParser.extract_attachment_ids(json_data)
            for att_id in attachments:
                if att_id in vfs:
                    protected_file_ids.add(att_id)
            protected_file_ids.add(item_id)
            # Определяем папку чата и обработку переименования
            target_chat_folder_id, safe_title = self.resolve_chat_folder(
                vfs, item_id, item_name, allocated_chat_names
            )
            # Перенос самого файла чата
            current_parent = item.get('parents', [None])[0]
            move_needed = current_parent != target_chat_folder_id
            rename_needed = item_name != safe_title
            if move_needed and rename_needed:
                self.transaction_plan["move_items"].append({
                    "item_id": item_id,
                    "new_parent_id": target_chat_folder_id,
                    "new_name": safe_title
                })
            elif move_needed:
                self.transaction_plan["move_items"].append({
                    "item_id": item_id,
                    "new_parent_id": target_chat_folder_id
                })
                vfs[item_id]['parents'] = [target_chat_folder_id]
            # Переименование самого файла чата, если его имя отличается от safe_title
            elif rename_needed:
                self.transaction_plan["move_items"].append({
                    "item_id": item_id,
                    "new_name": safe_title
                })
                vfs[item_id]['name'] = safe_title
            # Перенос заметки в папку чата и защита этой заметки
            if existing_md_id:
                protected_file_ids.add(existing_md_id)
                md_parent = vfs[existing_md_id].get('parents', [None])[0]
                if md_parent != target_chat_folder_id:
                    self.transaction_plan["move_items"].append({
                        "item_id": existing_md_id,
                        "new_parent_id": target_chat_folder_id
                    })
                    vfs[existing_md_id]['parents'] = [target_chat_folder_id]

            last_updated_str = map_info.get("modifiedTime", "") if isinstance(map_info, dict) else map_info
            last_thoughts_flag = map_info.get("thoughts_needed", None) if isinstance(map_info, dict) else None
            # Чат нуждается в обновлении, если изменилась дата ИЛИ изменилась настройка мыслей ИИ!
            needs_update = ((item_id not in self.map_data)
                            or (item['modifiedTime'] > last_updated_str)
                            or (last_thoughts_flag != thoughts_needed)
                            or not existing_md_id)
            # Задача на генерацию Markdown
            if needs_update:
                # Симуляция вложений
                resolved_files = self.simulate_attachments(
                    vfs, target_chat_folder_id, attachments, protected_file_ids, claimed_attachments, drive_att_cache
                )
                success, msg = ChatParser.convert_chat_to_md(
                    json_data=json_data,
                    chat_title=safe_title,
                    file_id=item_id,
                    resolved_files=resolved_files,
                    thoughts_needed=thoughts_needed
                )
                if success:
                    self.transaction_plan["upload_files"].append({
                        "chat_id": item_id,
                        "name": f"{safe_title}.md",
                        "parent_id": target_chat_folder_id,
                        "content": msg,
                        "file_id": existing_md_id  # Если None - создастся новый файл, если есть ID - обновится
                    })
                else:
                    logging.error(f"Для чата {item_name} парсер не сработал!!!\n{msg}")
        # 4. Расчет сборщика мусора
        self.run_mark_and_sweep(vfs, protected_file_ids, system_names)

        for _ in range(2): logging.info("")
        logging.info(f"Всего чатов в `Google AI Studio`: {len(json_chats)}")
        logging.info(f"Всего файлов и папок в `Google AI Studio`: {len(all_items)}\n")
        logging.info(f"ПЛАН РАБОТЫ:")
        logging.info(f"Создать папок:        {len(self.transaction_plan['create_folders'])}")
        logging.info(f"Переместить файлов:   {len(self.transaction_plan['move_items'])}")
        logging.info(f"Загрузить md заметок: {len(self.transaction_plan['upload_files'])}")
        logging.info(f"Переместить в _Trash: {len(self.transaction_plan['trash_items'])}")

    def phase_2_execute(self):
        logging.info("ФАЗА 2: Исполнение транзакционного плана")
        resolved_temp_ids = {}
        # 1. СОЗДАНИЕ ПАПОК (Асинхронный граф зависимостей)
        if self.transaction_plan["create_folders"]:
            logging.info("Создание новых папок (в параллельных потоках)...")
            # Строим карту зависимостей
            tasks_by_temp_id = {t["temp_id"]: t for t in self.transaction_plan["create_folders"]}
            children_map = {t["temp_id"]: [] for t in self.transaction_plan["create_folders"]}
            pending_dependencies = {t["temp_id"]: 0 for t in self.transaction_plan["create_folders"]}
            import queue
            ready_queue = queue.Queue()
            # Вычисляем, кто от кого зависит
            for task in self.transaction_plan["create_folders"]:
                p_id = task["parent_id"]
                if p_id in tasks_by_temp_id:
                    # Родитель тоже создается в этой сессии, мы от него зависим
                    children_map[p_id].append(task["temp_id"])
                    pending_dependencies[task["temp_id"]] += 1
                else:
                    # Родитель уже существует (реальный ID), можно создавать сразу!
                    ready_queue.put(task["temp_id"])
            resolved_lock = threading.Lock()
            def _create_folder_worker():
                while True:
                    # Поток спит, пока в очереди не появится готовая к созданию папка
                    temp_id = ready_queue.get()
                    task = tasks_by_temp_id[temp_id]
                    # Безопасно получаем актуальный ID родителя
                    with resolved_lock:
                        real_parent = resolved_temp_ids.get(task["parent_id"], task["parent_id"])
                    try:
                        # Единственная долгая сетевая операция!
                        created_folder = self.api.create_folder(task["name"], real_parent)
                        logging.debug(f"Создана папка: '{task['name']}' (ID: {created_folder['id']})")
                        with resolved_lock: # Сохраняем реальный ID
                            resolved_temp_ids[temp_id] = created_folder["id"]
                        for child_id in children_map[temp_id]: # Разблокируем дочерние папки
                            with resolved_lock:
                                pending_dependencies[child_id] -= 1
                                if pending_dependencies[child_id] == 0:
                                    ready_queue.put(child_id)
                    except Exception as e:
                        logging.error(f"Ошибка создания папки '{task['name']}': ", exc_info=True)
                        # Если родитель упал, снимаем зависшие дочерние задачи, чтобы не повесить join()!
                        with resolved_lock:
                            for child_id in children_map[temp_id]:
                                pending_dependencies[child_id] = -1
                finally:
                    finally:
                        # Сообщаем очереди, что задача выполнена
                        ready_queue.task_done()

            for _ in range(max_workers):
                t = threading.Thread(target=_create_folder_worker, daemon=True)
                t.start()

            # Главный поток просто ждет, пока вся очередь (весь граф) не будет обработана
            ready_queue.join()

        # 2. ПЕРЕМЕЩЕНИЕ И ПЕРЕИМЕНОВАНИЕ ОБЪЕКТОВ (параллельно)
        if self.transaction_plan["move_items"]:
            logging.info("Перемещение и переименование файлов...")

            def _exec_move(task):
                real_parent = resolved_temp_ids.get(task.get("new_parent_id"), task.get("new_parent_id")) \
                    if task.get("new_parent_id") else None
                self.api.update_item(
                    item_id=task["item_id"],
                    new_parent_id=real_parent,
                    new_name=task.get("new_name")
                )

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_exec_move, task) for task in self.transaction_plan["move_items"]]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        logging.error("Ошибка при перемещении и/или переименовании объекта: ", exc_info=True)

        # 3. ЗАГРУЗКА И ОБНОВЛЕНИЕ MARKDOWN ЗАМЕТОК (Параллельно)
        created_notes_map = {}
        if self.transaction_plan["upload_files"]:
            logging.info("Загрузка .md заметок...")

            def _exec_upload(task):
                real_parent = resolved_temp_ids.get(task["parent_id"], task["parent_id"])
                uploaded = self.api.upload_file(
                    task["name"], real_parent, task["content"], mime_type="text/plain", file_id=task["file_id"]
                )
                return task["chat_id"], uploaded["id"]

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_exec_upload, task) for task in self.transaction_plan["upload_files"]]
                for future in as_completed(futures):
                    try:
                        chat_id, real_md_id = future.result()
                        created_notes_map[chat_id] = real_md_id
                    except Exception:
                        logging.error("Ошибка при загрузке .md заметки: ", exc_info=True)

        # 4. СБОРКА МУСОРА В _TRASH (Параллельно)
        if self.transaction_plan["trash_items"]:
            logging.info("Перемещение мусора в _Trash...")
            trash_id = self.sys_folders["trash_id"]

            def _exec_trash(task):
                self.api.update_item(item_id=task["item_id"], new_parent_id=trash_id)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_exec_trash, task) for task in self.transaction_plan["trash_items"]]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        logging.error("Ошибка при перемещении в _Trash: ", exc_info=True)

        # 5. СОХРАНЕНИЕ И ОБНОВЛЕНИЕ _MAP.JSON
        logging.info("Сохранение обновленной карты _map.json в облако...")
        final_map = dict(self.map_data)
        for item in self.transaction_plan["chats_to_map"]:
            chat_id = item["chat_id"]
            modtime = item["modifiedTime"]
            thoughts_needed = item["thoughts_needed"]
            note_id = created_notes_map.get(chat_id, item.get("note_id"))
            final_map[chat_id] = {
                "modifiedTime": modtime,
                "note_id": note_id,
                "thoughts_needed": thoughts_needed
            }

        map_json_str = json.dumps(final_map, ensure_ascii=False, indent=2)
        self.api.upload_file(
            "_map.json",
            self.sys_folders["studio_id"],
            map_json_str,
            mime_type="application/json",
            file_id=self.sys_folders["map_id"]
        )
        logging.info("Синхронизация успешно завершена!")


if __name__ == "__main__":
    engine = AppLogic()
    engine.run()
