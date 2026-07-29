import json
from datetime import datetime

class ChatParser:
    @staticmethod
    def parse_time(time_str):
        # Переводит время из формата UTC в локальный часовой пояс
        if not time_str:
            return datetime.now().astimezone()
        dt_obj = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        return dt_obj.astimezone()

    @staticmethod
    def format_callout(text, callout_header):
        # Форматирует текст для вставки внутрь выноски Obsidian
        if not text:
            return ""
        formatted = "\n".join(f"> {line}" for line in text.split("\n"))
        return f"> {callout_header}\n{formatted}"

    @staticmethod
    def extract_attachment_ids(json_data):
        ids = []
        for chunk in json_data.get("chunkedPrompt", {}).get("chunks", []):
            for key, value in chunk.items():
                if isinstance(value, dict) and "id" in value:
                    ids.append(value["id"])
        return ids

    @staticmethod
    def is_a_chat(json_data):
        if not isinstance(json_data, dict) or not all(
                k in json_data for k in ("runSettings", "systemInstruction", "chunkedPrompt")):
            return False, "Файл не содержит обязательных атрибутов чата AI Studio"
        return True, "Данный JSON является чатом Google AI Studio"

    @staticmethod
    def convert_chat_to_md(json_data, chat_title, file_id=None, resolved_files=None, thoughts_needed=True):
        is_valid, msg = ChatParser.is_a_chat(json_data)
        if not is_valid:
            return False, msg
        return ChatParser.convert_json_to_md(json_data, chat_title, file_id, resolved_files, thoughts_needed)

    @staticmethod
    def convert_json_to_md(json_data, chat_title, file_id, resolved_files, thoughts_needed):
        grouped_msgs = []
        curr_msg = None
        try:
            chunks = json_data.get("chunkedPrompt", {}).get("chunks")
            if not chunks:
                return False, "В чате отсутствуют сообщения (chunks пуст)"
            for chunk in chunks:
                role = chunk.get("role", "system")
                chunk_time = ChatParser.parse_time(chunk.get("createTime"))

                # Группируем сообщения одной роли, идущие подряд
                if curr_msg is None or curr_msg["role"] != role:
                    if curr_msg:
                        grouped_msgs.append(curr_msg)
                    curr_msg = {"role": role, "time": chunk_time, "files": [], "isThought": [], "text": []}

                # Обновляем время группы сообщений до самого последнего чанка
                curr_msg["time"] = max(curr_msg["time"], chunk_time)

                # Собираем информацию о вложениях
                for key, value in chunk.items():
                    if isinstance(value, dict) and "id" in value:
                        curr_msg["files"].append({
                            "type": key,
                            "id": value["id"]
                        })

                # Разделяем мысли ИИ и его финальный ответ
                text = (chunk.get("text") or "").strip()
                if text:
                    if chunk.get("isThought"):
                        curr_msg["isThought"].append(text)
                    else:
                        curr_msg["text"].append(text)

            if curr_msg:
                grouped_msgs.append(curr_msg)

            if not grouped_msgs:
                return False, "В чате нет распознанных текстовых сообщений"

            first_time = grouped_msgs[0]["time"].isoformat(timespec='milliseconds')
            last_time = grouped_msgs[-1]["time"].isoformat(timespec='milliseconds')
            date_str = grouped_msgs[0]["time"].strftime("%Y-%m-%d")

            md_lines = []

            # --- Генерируем метаданные YAML ---
            md_lines.append("---")
            if file_id:
                md_lines.append(f"web-session: https://aistudio.google.com/app/prompts/{file_id}")
            md_lines.append("type: ai-studio-session")
            safe_yaml_title = json.dumps(chat_title, ensure_ascii=False)
            md_lines.append(f"title: {safe_yaml_title}")
            md_lines.append(f"created: {first_time}")
            md_lines.append(f"last_active: {last_time}")
            md_lines.append("metadata:")
            md_lines.append("  autoConverted: true")
            md_lines.append("---")
            md_lines.append("")  # Пустая строка после фронтметтера

            md_lines.append(f"# Agent Session {date_str}")
            md_lines.append("")  # Пустая строка после заголовка H1

            # --- Генерируем тело заметки ---
            for msg in grouped_msgs:
                role = msg["role"]
                speaker = role if role == "user" else "model"
                callout = "[!user]+" if role == "user" else "[!assistant]+"

                md_lines.append(f"## {speaker}")
                md_lines.append("")  # Пустая строка после заголовка H2

                # 1. Колаут с метаданными
                md_lines.append("> [!metadata]- Message Info")
                md_lines.append(f"> Time: {msg['time'].isoformat(timespec='milliseconds')}")
                md_lines.append("")  # ОБЯЗАТЕЛЬНАЯ пустая строка, чтобы отцепить следующий колаут

                # 2. Колаут с вложениями
                if msg["files"]:
                    md_lines.append("> [!attachments]+")
                    md_lines.append(">")  # Визуальный отступ внутри колаута перед таблицей
                    md_lines.append("> | Attachment | ID | Local Link | Web Link |")
                    md_lines.append("> | ---------- | -- | ---------- | -------- |")

                    for f in msg["files"]:
                        f_id = f["id"]
                        info = resolved_files.get(f_id) if resolved_files else None
                        status = info.get("status") if info else None
                        drive_url = f"https://drive.google.com/file/d/{f_id}/view"
                        web_link = f"[Open in Drive]({drive_url})"

                        if status == "active_local":
                            orig_name = info["name"]
                            ext = info.get("ext", "unknown")
                            local_link = f"[[Attachments/{ext}/{orig_name}]]"
                        elif status == "active_drive":
                            local_link = "From My Drive"
                        else:
                            local_link = "-"
                            web_link = "Файл удален"

                        md_lines.append(f"> | {f['type']} | {f_id} | {local_link} | {web_link} |")

                    md_lines.append("")  # ОБЯЗАТЕЛЬНАЯ пустая строка после вложений

                # 3. Колаут с рассуждениями (Thoughts)
                if msg["isThought"] and thoughts_needed:
                    combined_thoughts = "\n\n".join(msg["isThought"])
                    md_lines.append(ChatParser.format_callout(combined_thoughts, "[!reasoning]-"))
                    md_lines.append("")  # ОБЯЗАТЕЛЬНАЯ пустая строка перед основным ответом

                # 4. Колаут с основным текстом
                if msg["text"]:
                    combined_text = "\n\n".join(msg["text"])
                    md_lines.append(ChatParser.format_callout(combined_text, callout))
                    md_lines.append("")  # Пустая строка перед разделителем сообщений

                md_lines.append("---")
                md_lines.append("")  # Пустая строка перед следующим сообщением (заголовком H2)

            # Соединяем чистый список строк одним переносом
            return True, "\n".join(md_lines)
        except Exception as e:
            return False, f"Ошибка при генерации разметки: {e}"
