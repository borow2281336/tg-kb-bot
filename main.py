import os, re, time, json, asyncio, base64
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

import requests
import pytesseract
from pdf2image import convert_from_path
from langdetect import detect, DetectorFactory
import yake
from wordfreq import zipf_frequency

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


DetectorFactory.seed = 0


INBOX_DIR = Path(os.getenv("INBOX_DIR", "./inbox"))
TEXT_DIR = Path(os.getenv("TEXT_DIR", "./extracted_text"))
INBOX_DIR.mkdir(parents=True, exist_ok=True)
TEXT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}


def detect_language(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if len(t) < 30:
        return "unknown"
    try:
        return detect(t)
    except Exception:
        return "unknown"


def safe_ext(filename: str) -> str:
    return Path(filename).suffix.lower().strip()


def guess_type(filename: str, mime_type: str | None) -> str:
    ext = safe_ext(filename)
    return ext.lstrip(".") if ext else (mime_type or "unknown")


def extract_text_native(path: Path) -> dict:
    ext = path.suffix.lower()
    meta = {"method": None, "pages": None, "chars": 0}

    if ext in {".txt", ".md"}:
        text = path.read_text(errors="ignore")
        meta.update(method="plain_read", chars=len(text))
        return {"text": text, "meta": meta}

    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        text = "\n".join(p.text for p in d.paragraphs)
        meta.update(method="python-docx", chars=len(text))
        return {"text": text, "meta": meta}

    if ext == ".pdf":
        from pypdf import PdfReader
        r = PdfReader(str(path))
        chunks = [(p.extract_text() or "") for p in r.pages]
        text = "\n".join(chunks)
        meta.update(method="pypdf", pages=len(r.pages), chars=len(text))
        return {"text": text, "meta": meta}

    return {"text": "", "meta": {"method": "unsupported", "pages": None, "chars": 0}}


def ocr_pdf(path: Path, max_pages: int = 5, dpi: int = 200, lang: str = "rus+eng") -> dict:
    images = convert_from_path(str(path), dpi=dpi, first_page=1, last_page=max_pages)
    text = "\n".join(pytesseract.image_to_string(img, lang=lang) for img in images)
    return {"text": text, "meta": {"method": "tesseract_ocr", "pages": len(images), "chars": len(text)}}


def extract_text_with_ocr(path: Path, ocr_threshold_chars: int = 250, max_ocr_pages: int = 5) -> dict:
    native = extract_text_native(path)
    if path.suffix.lower() == ".pdf" and native["meta"]["chars"] < ocr_threshold_chars:
        return ocr_pdf(path, max_pages=max_ocr_pages)
    return native


def scrub_contacts(text: str) -> str:
    t = text or ""
    t = re.sub(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", " ", t)
    t = re.sub(r"@[\w_]{3,}", " ", t)
    t = re.sub(r"\+?\d[\d\s\-\(\)]{7,}\d", " ", t)
    t = re.sub(r"\b(?:e-?mail|тел\.?|телефон)\b\s*[:\-]?\s*", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def simple_summary(text: str, max_chars: int = 900) -> str:
    t = scrub_contacts(text)
    if not t:
        return "—"
    parts = re.split(r"(?<=[.!?])\s+", t)
    s = " ".join(parts[:3]).strip() if parts else t
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"
    return s


def hf_summarize(text: str, model: str = "facebook/bart-large-cnn", max_length: int = 160, min_length: int = 40) -> str:
    token = os.getenv("HF_TOKEN", "").strip()
    if not token:
        return simple_summary(text)

    url = f"https://router.huggingface.co/hf-inference/models/{model}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "inputs": (text or "")[:12000],
        "parameters": {"max_length": max_length, "min_length": min_length, "do_sample": False},
        "options": {"wait_for_model": True},
    }

    for i in range(4):
        r = requests.post(url, headers=headers, json=payload, timeout=180)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data and isinstance(data[0], dict) and "summary_text" in data[0]:
                return data[0]["summary_text"].strip()
            if isinstance(data, dict) and "summary_text" in data:
                return str(data["summary_text"]).strip()
            return str(data).strip()

        if r.status_code == 503:
            try:
                j = r.json()
                if isinstance(j, dict) and "estimated_time" in j:
                    time.sleep(float(j["estimated_time"]) + 1.0)
                    continue
            except Exception:
                pass

        time.sleep(1.5 * (i + 1))

    return simple_summary(text)


def yake_keywords_clean(text: str, lang: str = "en", top_k: int = 8) -> list:
    lang = lang if lang in {"en", "ru", "de", "fr", "es", "it", "pt"} else "en"
    clean = scrub_contacts(text)

    tokens = re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", clean)
    freq = Counter(t.lower() for t in tokens)

    kw_extractor = yake.KeywordExtractor(lan=lang, n=2, top=top_k * 3)
    kws = kw_extractor.extract_keywords(clean)

    def is_gibberish_word(w: str) -> bool:
        wl = w.lower()
        if len(wl) < 4:
            return False
        if re.fullmatch(r"[a-z]+", wl):
            z = zipf_frequency(wl, "en")
            if z < 2.5 and freq.get(wl, 0) <= 1:
                return True
        return False

    out = []
    for kw, _ in kws:
        kw = " ".join(kw.split()).strip()
        if not kw:
            continue
        words = re.findall(r"[A-Za-zА-Яа-яЁё]+", kw)
        if any(is_gibberish_word(w) for w in words):
            continue
        if kw.lower() not in [x.lower() for x in out]:
            out.append(kw)

    out = out[:10]
    if len(out) < 5:
        out = (out + out)[:5]
    return out


def summarize_and_keywords(text: str, lang_hint: str) -> dict:
    clean = scrub_contacts(text)
    if lang_hint == "en" and len(clean) >= 300:
        summary = hf_summarize(clean)
    else:
        summary = simple_summary(clean)
    keywords = yake_keywords_clean(clean, lang=lang_hint, top_k=8)
    return {"summary": summary, "keywords": keywords}


def load_gsheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    path = os.getenv("SERVICE_JSON_PATH", "").strip()
    b64 = os.getenv("SERVICE_JSON_B64", "").strip()

    if not path and b64:
        tmp = Path("/tmp/service.json")
        tmp.write_bytes(base64.b64decode(b64))
        path = str(tmp)

    if not path:
        raise RuntimeError("SERVICE_JSON_PATH or SERVICE_JSON_B64 is required")

    creds = Credentials.from_service_account_file(path, scopes=scopes)
    return gspread.authorize(creds)


def ensure_headers(ws):
    headers = [
        "timestamp",
        "uploader",
        "file_name",
        "summary",
        "keywords",
        "language",
        "file_size_bytes",
        "file_type",
        "text_extract_method",
        "text_pages",
        "text_chars",
        "message_link",
        "note",
        "local_path",
        "text_path",
    ]
    if ws.row_values(1) != headers:
        ws.clear()
        ws.append_row(headers)
    return headers


def append_record(ws, record: dict):
    row = [
        record.get("timestamp"),
        record.get("uploader"),
        record.get("file_name"),
        record.get("summary"),
        record.get("keywords"),
        record.get("language"),
        record.get("file_size_bytes"),
        record.get("file_type"),
        record.get("text_extract_method"),
        record.get("text_pages"),
        record.get("text_chars"),
        record.get("message_link"),
        record.get("note"),
        record.get("local_path"),
        record.get("text_path"),
    ]
    ws.append_row(row, value_input_option="RAW")


def format_reply(record: dict, sheets_status: str) -> str:
    size_kb = (int(record.get("file_size_bytes") or 0)) / 1024
    pages = record.get("text_pages")
    pages_str = str(pages) if pages is not None else "—"

    summary = (record.get("summary") or "").strip()
    if len(summary) > 700:
        summary = summary[:700].rstrip() + "…"

    kw = (record.get("keywords") or "").strip() or "—"
    link = record.get("message_link")
    note = (record.get("note") or "").strip()

    lines = [
        "✅ *Файл обработан и сохранён*",
        "",
        f"📄 *Файл:* `{record.get('file_name','')}`",
        f"👤 *Отправитель:* {record.get('uploader','')}",
        f"🕒 *Время (UTC):* `{record.get('timestamp','')}`",
        f"📦 *Размер:* `{size_kb:.1f} KB`",
        f"🧾 *Тип:* `{record.get('file_type','')}`",
        f"🔎 *Текст:* `{record.get('text_extract_method','')}`, стр.: `{pages_str}`, символов: `{record.get('text_chars','')}`",
        f"🌍 *Язык:* `{record.get('language','')}`",
    ]
    if link:
        lines.append(f"🔗 *Сообщение:* {link}")
    if note:
        lines.append(f"🗒️ *Заметка:* {note}")

    lines += [
        "",
        "📝 *Краткое содержание:*",
        summary or "—",
        "",
        "🏷️ *Ключевые слова:*",
        kw,
        "",
        f"📊 *Google Sheets:* {sheets_status}",
    ]
    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Пришли PDF/DOCX/TXT/MD.\n"
        "Я извлеку текст (OCR при необходимости), сделаю summary/keywords и сохраню в Google Sheets."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document

    file_name = doc.file_name or doc.file_unique_id
    ext = safe_ext(file_name)
    if ext not in ALLOWED_EXT:
        await msg.reply_text("❌ Поддерживаются только PDF/DOCX/TXT/MD.")
        return

    tg_file = await context.bot.get_file(doc.file_id)
    local_path = INBOX_DIR / f"{doc.file_unique_id}_{file_name}"
    await tg_file.download_to_drive(custom_path=str(local_path))

    parsed = extract_text_with_ocr(local_path, ocr_threshold_chars=250, max_ocr_pages=5)
    full_text = parsed["text"]
    meta = parsed["meta"]

    text_path = TEXT_DIR / f"{doc.file_unique_id}.txt"
    text_path.write_text(full_text, errors="ignore")

    lang = detect_language(full_text[:4000])
    lang_hint = lang if lang in {"en", "ru"} else "en"

    chat_username = getattr(msg.chat, "username", None)
    message_link = f"https://t.me/{chat_username}/{msg.message_id}" if chat_username else ""

    note = (msg.caption or "").strip()
    uploader = f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else str(msg.from_user.id)

    ai = await asyncio.to_thread(summarize_and_keywords, full_text, lang_hint)
    summary = ai["summary"]
    keywords = ", ".join(ai["keywords"])

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uploader": uploader,
        "file_name": file_name,
        "summary": summary,
        "keywords": keywords,
        "language": lang,
        "file_size_bytes": doc.file_size,
        "file_type": guess_type(file_name, doc.mime_type),
        "text_extract_method": meta.get("method"),
        "text_pages": meta.get("pages"),
        "text_chars": meta.get("chars"),
        "message_link": message_link,
        "note": note,
        "local_path": str(local_path),
        "text_path": str(text_path),
    }

    sheets_status = "appended ✅"
    try:
        ws = context.application.bot_data["ws"]
        append_record(ws, record)
    except Exception:
        sheets_status = "failed ❌"

    await msg.reply_text(format_reply(record, sheets_status), parse_mode="Markdown")
    print(json.dumps(record, ensure_ascii=False, indent=2))


def main():
    tg_token = os.getenv("TG_TOKEN", "").strip()
    sheet_url = os.getenv("SHEET_URL", "").strip()

    if not tg_token:
        raise RuntimeError("TG_TOKEN is required")
    if not sheet_url:
        raise RuntimeError("SHEET_URL is required")

    gc = load_gsheets_client()
    sh = gc.open_by_url(sheet_url)
    ws = sh.sheet1
    ensure_headers(ws)

    app = Application.builder().token(tg_token).build()
    app.bot_data["ws"] = ws

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("RUNNING...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
