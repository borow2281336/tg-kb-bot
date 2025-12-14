# AI helper bot: Telegram → OCR/текст → Summary/Keywords → Google Sheets

Пользователь отправляет документ в Telegram, бот извлекает текст (OCR для сканов), делает краткую выжимку и ключевые слова и сохраняет результат в Google Sheets.

## Что сделано
1) Telegram
- Бот принимает файлы: **PDF / DOCX / TXT / MD**
- Сохраняет метаданные: `file_name`, `file_size_bytes`, `mime/type/ext`, `username/user_id/chat_id`, `timestamp`, язык

2) Извлечение текста (+ OCR)
- TXT/MD: чтение файла
- DOCX: `python-docx`
- PDF: `pypdf`
- Если PDF “скан” (мало текста) → OCR: `pdf2image + pytesseract + tesseract (rus+eng)`

3) Суммаризация и ключевые слова
- Summary: Hugging Face Inference (router) + fallback при ошибках
- Keywords: локально через `YAKE` (5–10 ключевых фраз)

4) Google Sheets
- Запись строки в таблицу через Service Account (`gspread`)
- Поля таблицы включают минимум из ТЗ: `timestamp/received_at`, `uploader(username)`, `file_name`, `summary`, `keywords`
- Дополнительно: метод извлечения, язык, ссылки/заметки (если добавлены)

5) Демонстрация
- Проверено на 2–3 документах (txt, обычный pdf, pdf-скан с OCR) — строки появляются в Sheets.


## Идеи улучшений

- Эмбеддинги и поиск похожих документов

- Улучшение summary/keywords для русского (модель/LLM)

- Webhook деплой вместо polling

## Что сделано

- Авто-переключение на OCR для “сканов” PDF

- Стабильность summary (fallback при ошибках API)

### Docker (как запускал для “обособленной” работы)
Контейнер держит бота постоянно запущенным (пока контейнер работает):
```bash
docker build -t tg-kb-bot .
docker run --rm --env-file .env -v "$(pwd)/service.json:/app/service.json:ro" tg-kb-bot





