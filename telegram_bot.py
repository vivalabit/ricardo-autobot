import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, parse, request
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parent
PARSER_PATH = ROOT_DIR / "scrape-page.py"
SETTINGS_PATH = ROOT_DIR / "data" / "bot_settings.json"
RICARDO_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
CHECK_COMMAND_RE = re.compile(r"^\s*/check(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
LANGUAGE_COMMAND_RE = re.compile(r"^\s*/(?:lang|language)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
HELP_COMMAND_RE = re.compile(r"^\s*/(?:start|help)(?:@\w+)?\s*$", re.IGNORECASE)
DEFAULT_OPENCLAW_AGENT_ID = "ricardo-resale"
DEFAULT_OPENCLAW_TIMEOUT_SECONDS = 600
DEFAULT_TRANSCRIPT_WAIT_SECONDS = 12
DEFAULT_RESPONSE_LANGUAGE = "en"
TRANSCRIPT_POLL_INTERVAL_SECONDS = 0.5
TELEGRAM_MESSAGE_LIMIT = 3900
LANGUAGE_LABELS = {
    "en": "English",
    "ru": "Russian",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
}
LANGUAGE_ALIASES = {
    "en": "en",
    "english": "en",
    "eng": "en",
    "ru": "ru",
    "russian": "ru",
    "rus": "ru",
    "русский": "ru",
    "de": "de",
    "deutsch": "de",
    "german": "de",
    "fr": "fr",
    "francais": "fr",
    "français": "fr",
    "french": "fr",
    "it": "it",
    "italian": "it",
    "italiano": "it",
    "es": "es",
    "spanish": "es",
    "espanol": "es",
    "español": "es",
}


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


def send_message(token, chat_id, text, reply_to_message_id=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    telegram_request(token, "sendMessage", payload)


def send_chat_action(token, chat_id, action="typing"):
    try:
        telegram_request(token, "sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        logging.debug("Failed to send Telegram chat action", exc_info=True)


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


def format_error(prefix, exc):
    message = str(exc).strip() or exc.__class__.__name__
    return f"{prefix}: {message[:500]}"


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def build_agent_message(payload, user_text, check_argument, response_language):
    source = payload.get("source") or {}
    lot = payload.get("lot") or {}
    language_name = language_label(response_language)
    request_context = {
        "telegram_message": user_text,
        "check_arguments": check_argument,
        "lot_url": source.get("url"),
        "listing_id": source.get("listing_id"),
        "response_language": language_name,
    }

    return "\n".join(
        [
            "Analyze this Ricardo.ch lot as a Swiss resale opportunity.",
            "Use the provided lot JSON as factual input. You may do web research for market prices and sources.",
            f"Return only the final Telegram-ready answer in {language_name}. Do not return JSON.",
            "If the user supplied constraints such as min_profit, max_price, shipping, repair, or fee_rate, apply them.",
            "",
            "Request context:",
            json.dumps(request_context, ensure_ascii=False, indent=2),
            "",
            "Lot JSON:",
            json.dumps({"source": source, "lot": lot, "seller": payload.get("seller") or {}}, ensure_ascii=False, indent=2),
        ]
    )


def is_language_command(text):
    return bool(LANGUAGE_COMMAND_RE.match(text or ""))


def extract_language_argument(text):
    match = LANGUAGE_COMMAND_RE.match(text or "")
    return match.group(1).strip() if match and match.group(1) else ""


def is_help_command(text):
    return bool(HELP_COMMAND_RE.match(text or ""))


def normalize_language(value):
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
    return LANGUAGE_ALIASES.get(cleaned)


def language_label(code):
    return LANGUAGE_LABELS.get(code, LANGUAGE_LABELS[DEFAULT_RESPONSE_LANGUAGE])


def supported_language_text():
    return ", ".join(f"{code}={label}" for code, label in LANGUAGE_LABELS.items())


def language_keyboard():
    return {
        "keyboard": [
            [{"text": "English"}, {"text": "Russian"}],
            [{"text": "German"}, {"text": "French"}, {"text": "Italian"}, {"text": "Spanish"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def help_text(chat_id):
    current_language = language_label(get_chat_language(chat_id))
    return "\n".join(
        [
            "Send a Ricardo.ch lot link or use:",
            "/check <ricardo_lot_link> [min_profit=30] [max_price=180]",
            "",
            f"OpenClaw response language: {current_language}.",
            "Choose it before checking a lot with /language en, /language ru, /language de, /language fr, /language it, or /language es.",
        ]
    )


def language_help_text(chat_id):
    current_language = language_label(get_chat_language(chat_id))
    return "\n".join(
        [
            f"Current OpenClaw response language: {current_language}.",
            f"Supported languages: {supported_language_text()}.",
            "Use /language <code>, for example /language en or /language ru.",
        ]
    )


def load_bot_settings():
    if not SETTINGS_PATH.exists():
        return {}

    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("Failed to read bot settings from %s", SETTINGS_PATH, exc_info=True)
        return {}

    return settings if isinstance(settings, dict) else {}


def save_bot_settings(settings):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SETTINGS_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(SETTINGS_PATH)


def default_response_language():
    return normalize_language(os.getenv("OPENCLAW_RESPONSE_LANGUAGE")) or DEFAULT_RESPONSE_LANGUAGE


def get_chat_language(chat_id):
    settings = load_bot_settings()
    chat_settings = (settings.get("chats") or {}).get(str(chat_id)) or {}
    return normalize_language(chat_settings.get("language")) or default_response_language()


def set_chat_language(chat_id, language_code):
    settings = load_bot_settings()
    chats = settings.setdefault("chats", {})
    chat_settings = chats.setdefault(str(chat_id), {})
    chat_settings["language"] = language_code
    save_bot_settings(settings)


def openclaw_command_candidates():
    configured = os.getenv("OPENCLAW_AGENT_COMMAND") or os.getenv("OPENCLAW_BIN")
    if configured:
        return [shlex.split(configured)]

    return [
        ["openclaw"],
        [str(Path.home() / ".npm-global" / "bin" / "openclaw")],
    ]


def build_agent_session_id(agent_id, chat_id, message_id, payload):
    source = payload.get("source") or {}
    raw = "-".join(
        str(part)
        for part in [
            agent_id,
            chat_id,
            message_id,
            source.get("listing_id") or int(time.time()),
        ]
        if part is not None
    )
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw)[:120]


def run_openclaw_agent(message, *, chat_id, message_id, payload):
    agent_id = os.getenv("OPENCLAW_AGENT_ID", DEFAULT_OPENCLAW_AGENT_ID)
    agent_timeout = env_int("OPENCLAW_AGENT_TIMEOUT_SECONDS", DEFAULT_OPENCLAW_TIMEOUT_SECONDS)
    process_timeout = env_int("OPENCLAW_AGENT_PROCESS_TIMEOUT_SECONDS", agent_timeout + 60)
    deliver_reply = env_flag("OPENCLAW_AGENT_DELIVER_REPLY", False)
    session_id = build_agent_session_id(agent_id, chat_id, message_id, payload)

    args = [
        "agent",
        "--agent",
        agent_id,
        "--session-id",
        session_id,
        "--message",
        message,
        "--timeout",
        str(agent_timeout),
        "--json",
    ]

    model = os.getenv("OPENCLAW_AGENT_MODEL")
    if model:
        args.extend(["--model", model])

    thinking = os.getenv("OPENCLAW_AGENT_THINKING")
    if thinking:
        args.extend(["--thinking", thinking])

    if env_flag("OPENCLAW_AGENT_LOCAL", False):
        args.append("--local")

    if deliver_reply:
        args.extend(["--deliver", "--reply-channel", "telegram", "--reply-to", str(chat_id)])
        reply_account = os.getenv("OPENCLAW_AGENT_REPLY_ACCOUNT")
        if reply_account:
            args.extend(["--reply-account", reply_account])

    last_missing_error = None
    for command in openclaw_command_candidates():
        try:
            completed = subprocess.run(
                [*command, *args],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                timeout=process_timeout,
            )
        except FileNotFoundError as exc:
            last_missing_error = exc
            continue

        if completed.returncode != 0:
            failed_result = with_transcript_reply(
                with_agent_metadata(parse_openclaw_json_or_none(completed.stdout) or {}, agent_id, session_id),
                agent_id,
                session_id,
            )
            if agent_reply_text(failed_result):
                return failed_result

            message = last_error_line(completed.stderr) or last_error_line(completed.stdout) or "OpenClaw agent failed"
            raise RuntimeError(message)

        result = parse_openclaw_json(completed.stdout)
        if not isinstance(result, dict):
            raise RuntimeError("OpenClaw agent returned an unexpected JSON shape")
        return with_transcript_reply(with_agent_metadata(result, agent_id, session_id), agent_id, session_id)

    raise RuntimeError(f"OpenClaw command not found: {last_missing_error}")


def parse_openclaw_json_or_none(text):
    try:
        parsed = parse_openclaw_json(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_openclaw_json(text):
    text = (text or "").strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("OpenClaw agent did not return JSON")
        return json.loads(text[start : end + 1])


def with_transcript_reply(result, agent_id, session_id):
    if agent_reply_text(result):
        return result

    transcript_text = wait_latest_agent_transcript_text(agent_id, session_id)
    if not transcript_text:
        return result

    payloads = list(result.get("payloads") or [])
    payloads.append({"text": transcript_text})
    return {**result, "payloads": payloads}


def with_agent_metadata(result, agent_id, session_id):
    return {
        **result,
        "_openclaw_agent_id": agent_id,
        "_openclaw_session_id": session_id,
        "_openclaw_transcript_path": str(agent_transcript_path(agent_id, session_id)),
    }


def wait_latest_agent_transcript_text(agent_id, session_id):
    wait_seconds = max(0, env_int("OPENCLAW_AGENT_TRANSCRIPT_WAIT_SECONDS", DEFAULT_TRANSCRIPT_WAIT_SECONDS))
    deadline = time.monotonic() + wait_seconds

    while True:
        transcript_text = read_latest_agent_transcript_text(agent_id, session_id)
        if transcript_text:
            return transcript_text

        if time.monotonic() >= deadline:
            return None

        time.sleep(TRANSCRIPT_POLL_INTERVAL_SECONDS)


def agent_transcript_path(agent_id, session_id):
    return Path.home() / ".openclaw" / "agents" / agent_id / "sessions" / f"{session_id}.jsonl"


def read_latest_agent_transcript_text(agent_id, session_id):
    transcript_path = agent_transcript_path(agent_id, session_id)
    if not transcript_path.exists():
        return None

    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue

        text = assistant_message_text(message.get("content"))
        if text:
            return text

    return None


def assistant_message_text(content):
    if isinstance(content, str):
        return content.strip() or None

    if not isinstance(content, list):
        return None

    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts.append(text)

    return "\n\n".join(parts) if parts else None


def agent_reply_text(result):
    payloads = result.get("payloads") or []
    parts = []
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("text"):
            parts.append(str(payload["text"]))
        elif isinstance(payload, str):
            parts.append(payload)

    if not parts:
        meta = result.get("meta") or {}
        final_text = meta.get("finalAssistantVisibleText")
        if final_text:
            parts.append(str(final_text))

    return trim_telegram_message("\n\n".join(part.strip() for part in parts if part and part.strip()))


def should_send_agent_reply(result):
    if not env_flag("OPENCLAW_AGENT_DELIVER_REPLY", False):
        return True

    delivery_status = result.get("deliveryStatus") or {}
    return delivery_status.get("succeeded") is not True


def trim_telegram_message(text, limit=TELEGRAM_MESSAGE_LIMIT):
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def handle_message(token, message):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = message.get("text") or ""
    message_id = message.get("message_id")

    if is_help_command(text):
        send_message(token, chat_id, help_text(chat_id), message_id, language_keyboard())
        return

    if is_language_command(text):
        language_code = normalize_language(extract_language_argument(text))
        if not language_code:
            send_message(token, chat_id, language_help_text(chat_id), message_id, language_keyboard())
            return

        set_chat_language(chat_id, language_code)
        send_message(token, chat_id, f"OpenClaw response language set to {language_label(language_code)}.", message_id)
        return

    language_choice = normalize_language(text)
    if language_choice and not extract_ricardo_url(text):
        set_chat_language(chat_id, language_choice)
        send_message(token, chat_id, f"OpenClaw response language set to {language_label(language_choice)}.", message_id)
        return

    if is_check_command(text):
        check_argument = extract_check_argument(text)
        url = extract_ricardo_url(check_argument)
        if not url:
            send_message(
                token,
                chat_id,
                "Use: /check <ricardo_lot_link> [min_profit=30] [max_price=180]",
                message_id,
            )
            return
    else:
        url = extract_ricardo_url(text)
        check_argument = ""

    if not url:
        send_message(token, chat_id, "Send a Ricardo.ch lot link or use /check <ricardo_lot_link>.", message_id)
        return

    send_chat_action(token, chat_id)

    try:
        payload = run_parser(url)
    except Exception as exc:
        logging.exception("Failed to parse Ricardo URL: %s", url)
        send_message(token, chat_id, format_error("Failed to parse the lot", exc), message_id)
        return

    send_chat_action(token, chat_id)

    try:
        agent_result = run_openclaw_agent(
            build_agent_message(payload, text, check_argument, get_chat_language(chat_id)),
            chat_id=chat_id,
            message_id=message_id,
            payload=payload,
        )
    except Exception as exc:
        logging.exception("OpenClaw agent failed")
        send_message(token, chat_id, format_error("OpenClaw agent failed to evaluate the lot", exc), message_id)
        return

    if should_send_agent_reply(agent_result):
        reply_text = agent_reply_text(agent_result)
        if reply_text:
            send_message(token, chat_id, reply_text, message_id)
        else:
            session_id = agent_result.get("_openclaw_session_id") or "unknown"
            transcript_path = agent_result.get("_openclaw_transcript_path") or "unknown"
            logging.warning("OpenClaw returned no visible text: session_id=%s transcript=%s", session_id, transcript_path)
            send_message(
                token,
                chat_id,
                "OpenClaw finished without visible reply text. "
                f"session_id={session_id}",
                message_id,
            )


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
