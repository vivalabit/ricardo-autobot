import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, parse, request
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parent
PARSER_PATH = ROOT_DIR / "scrape-page.py"
RICARDO_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
CHECK_COMMAND_RE = re.compile(r"^\s*/check(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)


def load_env_file(path=ROOT_DIR / ".env"):
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def extract_ricardo_url(text):
    for match in RICARDO_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;)")
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        if host == "ricardo.ch" or host.endswith(".ricardo.ch"):
            return url

    return None


def extract_check_argument(text):
    match = CHECK_COMMAND_RE.match(text or "")
    return match.group(1).strip() if match and match.group(1) else ""


def is_check_command(text):
    return bool(re.match(r"^\s*/check(?:@\w+)?(?:\s|$)", text or "", re.IGNORECASE))


def telegram_request(token, method, payload):
    data = parse.urlencode(payload).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = request.Request(url, data=data)

    with request.urlopen(req, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8"))

    if not body.get("ok"):
        raise RuntimeError(body.get("description") or f"Telegram API error in {method}")

    return body.get("result")


def send_message(token, chat_id, text, reply_to_message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    telegram_request(token, "sendMessage", payload)


def run_parser(url):
    completed = subprocess.run(
        [sys.executable, str(PARSER_PATH), "--url", url, "--headless"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode != 0:
        raise RuntimeError(last_error_line(completed.stderr) or "parser failed")

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("parser did not return JSON") from exc


def last_error_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def format_success(payload):
    source = payload.get("source") or {}
    lot = payload.get("lot") or {}

    title = lot.get("title") or source.get("listing_id") or "Ricardo lot"
    lines = [f"Parsed: {title}"]

    if source.get("listing_id"):
        lines.append(f"ID: {source['listing_id']}")

    if lot.get("current_price_chf") is not None:
        lines.append(f"Price: CHF {lot['current_price_chf']}")

    return "\n".join(lines)


def format_error(exc):
    message = str(exc).strip() or exc.__class__.__name__
    return f"Parsing failed: {message[:500]}"


def handle_message(token, message):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = message.get("text") or ""
    message_id = message.get("message_id")

    if is_check_command(text):
        check_argument = extract_check_argument(text)
        url = extract_ricardo_url(check_argument)
        if not url:
            send_message(token, chat_id, "Usage: /check <ricardo_lot_link>", message_id)
            return
    else:
        url = extract_ricardo_url(text)

    if not url:
        send_message(token, chat_id, "Send a Ricardo lot link or use /check <ricardo_lot_link>.", message_id)
        return

    try:
        payload = run_parser(url)
    except Exception as exc:
        logging.exception("Failed to parse Ricardo URL: %s", url)
        send_message(token, chat_id, format_error(exc), message_id)
        return

    send_message(token, chat_id, format_success(payload), message_id)


def main():
    load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    offset = None
    logging.info("Telegram bot started")

    while True:
        payload = {
            "timeout": 30,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            updates = telegram_request(token, "getUpdates", payload) or []
        except (error.URLError, TimeoutError, RuntimeError) as exc:
            logging.warning("Telegram polling failed: %s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if message:
                try:
                    handle_message(token, message)
                except Exception:
                    logging.exception("Failed to handle Telegram message")


if __name__ == "__main__":
    main()
