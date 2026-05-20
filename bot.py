import json
import logging
import os
import re
import time
from urllib import error, parse, request
from urllib.parse import urlparse

from openclaw_client import (
    agent_reply_text,
    build_agent_message,
    build_find_agent_message,
    run_openclaw_agent,
    should_send_agent_reply,
)
from settings import (
    LANGUAGE_LABELS,
    default_response_language,
    get_chat_language,
    language_label,
    load_env_file,
    normalize_language,
    set_chat_language,
    supported_language_text,
)


RICARDO_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
CHECK_COMMAND_RE = re.compile(r"^\s*/check(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
FIND_COMMAND_RE = re.compile(r"^\s*/find(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
FIND_BUDGET_RE = re.compile(
    r"(?P<marker>(?:до|макс(?:имум)?|max(?:imum)?|under|up\s+to|<=|bis|unter|for|für|pour|jusqu[’']?a|jusqu[’']?à|moins\s+de)\s+)?"
    r"(?P<currency_before>(?:chf|sfr|francs?|franken|frs?\.?|франк(?:ов|а|и)?)\s+)?"
    r"(?P<amount>(?:\d{1,3}(?:[\s'’.,]\d{3})+|\d+(?:[.,]\d+)?))"
    r"(?:\s*(?P<currency_after>chf|sfr|francs?|franken|frs?\.?|франк(?:ов|а|и)?|\.-))?",
    re.IGNORECASE,
)
LANGUAGE_COMMAND_RE = re.compile(r"^\s*/(?:lang|language)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
HELP_COMMAND_RE = re.compile(r"^\s*/(?:start|help)(?:@\w+)?\s*$", re.IGNORECASE)
SETTINGS_COMMAND_RE = re.compile(r"^\s*/settings(?:@\w+)?\s*$", re.IGNORECASE)
LANGUAGE_CALLBACK_PREFIX = "language:"


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


def extract_find_argument(text):
    match = FIND_COMMAND_RE.match(text or "")
    return match.group(1).strip() if match and match.group(1) else ""


def is_find_command(text):
    return bool(re.match(r"^\s*/find(?:@\w+)?(?:\s|$)", text or "", re.IGNORECASE))


def parse_budget_amount(value):
    normalized = str(value or "").strip().replace("'", "").replace("’", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", normalized):
        normalized = re.sub(r"[.,]", "", normalized)
    elif "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")

    try:
        amount = float(normalized)
    except ValueError:
        return None

    if amount <= 0:
        return None

    return int(round(amount))


def parse_find_argument(argument):
    cleaned = re.sub(r"\s+", " ", (argument or "").strip())
    if not cleaned:
        return None

    budget_match = None
    for match in FIND_BUDGET_RE.finditer(cleaned):
        budget = parse_budget_amount(match.group("amount"))
        if not budget:
            continue
        budget_match = match

    if not budget_match:
        return None

    budget = parse_budget_amount(budget_match.group("amount"))
    item_query = f"{cleaned[:budget_match.start()]} {cleaned[budget_match.end():]}".strip(" ,;:-")
    item_query = re.sub(r"\s+", " ", item_query).strip()

    if not item_query:
        return None

    return {"item_query": item_query, "budget_chf": budget}


def is_language_command(text):
    return bool(LANGUAGE_COMMAND_RE.match(text or ""))


def extract_language_argument(text):
    match = LANGUAGE_COMMAND_RE.match(text or "")
    return match.group(1).strip() if match and match.group(1) else ""


def is_help_command(text):
    return bool(HELP_COMMAND_RE.match(text or ""))


def is_settings_command(text):
    return bool(SETTINGS_COMMAND_RE.match(text or ""))


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

    return telegram_request(token, "sendMessage", payload)


def edit_message_text(token, chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    return telegram_request(token, "editMessageText", payload)


def safe_edit_message_text(token, chat_id, message_id, text, reply_markup=None):
    if not message_id:
        return False

    try:
        edit_message_text(token, chat_id, message_id, text, reply_markup)
        return True
    except Exception:
        logging.debug("Failed to edit Telegram message", exc_info=True)
        return False


def answer_callback_query(token, callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True

    return telegram_request(token, "answerCallbackQuery", payload)


def set_bot_commands(token):
    commands = [
        {"command": "check", "description": "Check a Ricardo.ch lot"},
        {"command": "find", "description": "Find Ricardo.ch items under budget"},
        {"command": "language", "description": "Choose answer language"},
        {"command": "settings", "description": "Show current settings"},
        {"command": "help", "description": "Show help"},
    ]
    try:
        telegram_request(token, "setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)})
    except Exception:
        logging.warning("Failed to set Telegram bot commands", exc_info=True)


def progress_message_id(message):
    return message.get("message_id") if isinstance(message, dict) else None


def send_or_edit_message(token, chat_id, message_id, text, reply_to_message_id=None, reply_markup=None):
    if safe_edit_message_text(token, chat_id, message_id, text, reply_markup):
        return

    send_message(token, chat_id, text, reply_to_message_id, reply_markup)


def send_chat_action(token, chat_id, action="typing"):
    try:
        telegram_request(token, "sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        logging.debug("Failed to send Telegram chat action", exc_info=True)


def run_parser(url):
    from ricardo_parser import parse_ricardo_url

    return parse_ricardo_url(url, headless=True)


def run_search_parser(item_query, budget_chf):
    from ricardo_parser import parse_ricardo_search

    return parse_ricardo_search(item_query, budget_chf, headless=True)


def format_error(prefix, exc):
    message = str(exc).strip() or exc.__class__.__name__
    return f"{prefix}: {message[:500]}"


def language_keyboard(current_language=None):
    def button(code):
        label = language_label(code)
        if code == current_language:
            label = f"{label} *"
        return {"text": label, "callback_data": f"{LANGUAGE_CALLBACK_PREFIX}{code}"}

    codes = list(LANGUAGE_LABELS)
    return {
        "inline_keyboard": [
            [button(codes[0]), button(codes[1])],
            [button(codes[2]), button(codes[3])],
            [button(codes[4]), button(codes[5])],
        ],
    }


def help_text(chat_id):
    current_language = language_label(get_chat_language(chat_id))
    return "\n".join(
        [
            "Ricardo Assistant",
            "",
            "Send a Ricardo.ch lot link and I will check it as a Swiss resale opportunity.",
            "",
            "Commands:",
            "/check <ricardo_lot_link> [min_profit=30] [max_price=180]",
            "/find <item> <budget>",
            "/language - choose answer language",
            "/settings - show current settings",
            "/help - show this help",
            "",
            f"Current answer language: {current_language}.",
        ]
    )


def language_help_text(chat_id):
    current_language_code = get_chat_language(chat_id)
    current_language = language_label(current_language_code)
    return "\n".join(
        [
            "Answer language",
            "",
            f"Current: {current_language}.",
            f"Supported: {supported_language_text()}.",
            "",
            "Choose a language below or use /language <code>, for example /language en.",
        ]
    )


def settings_text(chat_id):
    current_language = language_label(get_chat_language(chat_id))
    default_language = language_label(default_response_language())
    return "\n".join(
        [
            "Settings",
            "",
            f"Answer language: {current_language}",
            f"Default language: {default_language}",
            "Language is saved per chat.",
            "",
            "Change language with the buttons below or /language <code>.",
            "Run checks with /check <ricardo_lot_link> [min_profit=30] [max_price=180].",
            "Find items with /find <item> <budget>, for example /find видеокарту до 500 франков.",
        ]
    )


def check_usage_text():
    return "\n".join(
        [
            "Use:",
            "/check <ricardo_lot_link> [min_profit=30] [max_price=180]",
            "",
            "You can also send a Ricardo.ch lot link directly.",
        ]
    )


def find_usage_text():
    return "\n".join(
        [
            "Use:",
            "/find <item> <budget>",
            "",
            "Examples:",
            "/find видеокарту до 500 франков",
            "/find RTX 4070 500 CHF",
        ]
    )


def check_progress_text(step):
    messages = {
        "parsing": "Processing Ricardo lot...\n\n[1/3] Reading the lot page.",
        "analyzing": "Processing Ricardo lot...\n\n[2/3] Running resale analysis and market research.",
        "finalizing": "Processing Ricardo lot...\n\n[3/3] Preparing the Telegram answer.",
        "delivered": "Analysis finished. OpenClaw sent the answer to this chat.",
    }
    return messages[step]


def find_progress_text(step):
    messages = {
        "searching": "Searching Ricardo...\n\n[1/3] Reading live Ricardo search results.",
        "analyzing": "Searching Ricardo...\n\n[2/3] Preparing links and short descriptions.",
        "finalizing": "Searching Ricardo...\n\n[3/3] Preparing the Telegram answer.",
        "delivered": "Search finished. OpenClaw sent the answer to this chat.",
    }
    return messages[step]


def handle_message(token, message):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = message.get("text") or ""
    message_id = message.get("message_id")

    if is_help_command(text):
        send_message(token, chat_id, help_text(chat_id), message_id, language_keyboard(get_chat_language(chat_id)))
        return

    if is_settings_command(text):
        send_message(token, chat_id, settings_text(chat_id), message_id, language_keyboard(get_chat_language(chat_id)))
        return

    if is_language_command(text):
        language_code = normalize_language(extract_language_argument(text))
        if not language_code:
            send_message(
                token,
                chat_id,
                language_help_text(chat_id),
                message_id,
                language_keyboard(get_chat_language(chat_id)),
            )
            return

        set_chat_language(chat_id, language_code)
        send_message(
            token,
            chat_id,
            settings_text(chat_id),
            message_id,
            language_keyboard(language_code),
        )
        return

    language_choice = normalize_language(text)
    if language_choice and not extract_ricardo_url(text):
        set_chat_language(chat_id, language_choice)
        send_message(
            token,
            chat_id,
            settings_text(chat_id),
            message_id,
            language_keyboard(language_choice),
        )
        return

    if is_find_command(text):
        find_request = parse_find_argument(extract_find_argument(text))
        if not find_request:
            send_message(token, chat_id, find_usage_text(), message_id)
            return

        item_query = find_request["item_query"]
        budget_chf = find_request["budget_chf"]

        send_chat_action(token, chat_id)
        progress_message = send_message(token, chat_id, find_progress_text("searching"), message_id)
        status_message_id = progress_message_id(progress_message)

        try:
            payload = run_search_parser(item_query, budget_chf)
        except Exception as exc:
            logging.exception("Failed to parse Ricardo search: query=%s budget=%s", item_query, budget_chf)
            send_or_edit_message(
                token,
                chat_id,
                status_message_id,
                format_error("Failed to search live Ricardo listings", exc),
                message_id,
            )
            return

        send_chat_action(token, chat_id)
        safe_edit_message_text(token, chat_id, status_message_id, find_progress_text("analyzing"))

        try:
            agent_result = run_openclaw_agent(
                build_find_agent_message(item_query, budget_chf, text, get_chat_language(chat_id), payload),
                chat_id=chat_id,
                message_id=message_id,
                payload=payload,
            )
        except Exception as exc:
            logging.exception("OpenClaw agent failed during Ricardo search")
            send_or_edit_message(
                token,
                chat_id,
                status_message_id,
                format_error("OpenClaw agent failed to search Ricardo", exc),
                message_id,
            )
            return

        safe_edit_message_text(token, chat_id, status_message_id, find_progress_text("finalizing"))

        if should_send_agent_reply(agent_result):
            reply_text = agent_reply_text(agent_result)
            if reply_text:
                send_or_edit_message(token, chat_id, status_message_id, reply_text, message_id)
            else:
                session_id = agent_result.get("_openclaw_session_id") or "unknown"
                transcript_path = agent_result.get("_openclaw_transcript_path") or "unknown"
                logging.warning(
                    "OpenClaw returned no visible search text: session_id=%s transcript=%s",
                    session_id,
                    transcript_path,
                )
                send_or_edit_message(
                    token,
                    chat_id,
                    status_message_id,
                    "OpenClaw finished without visible reply text. "
                    f"session_id={session_id}",
                    message_id,
                )
        else:
            send_or_edit_message(token, chat_id, status_message_id, find_progress_text("delivered"), message_id)

        return

    if is_check_command(text):
        check_argument = extract_check_argument(text)
        url = extract_ricardo_url(check_argument)
        if not url:
            send_message(token, chat_id, check_usage_text(), message_id)
            return
    else:
        url = extract_ricardo_url(text)
        check_argument = ""

    if not url:
        send_message(token, chat_id, check_usage_text(), message_id)
        return

    send_chat_action(token, chat_id)
    progress_message = send_message(token, chat_id, check_progress_text("parsing"), message_id)
    status_message_id = progress_message_id(progress_message)

    try:
        payload = run_parser(url)
    except Exception as exc:
        logging.exception("Failed to parse Ricardo URL: %s", url)
        send_or_edit_message(
            token,
            chat_id,
            status_message_id,
            format_error("Failed to parse the lot", exc),
            message_id,
        )
        return

    send_chat_action(token, chat_id)
    safe_edit_message_text(token, chat_id, status_message_id, check_progress_text("analyzing"))

    try:
        agent_result = run_openclaw_agent(
            build_agent_message(payload, text, check_argument, get_chat_language(chat_id)),
            chat_id=chat_id,
            message_id=message_id,
            payload=payload,
        )
    except Exception as exc:
        logging.exception("OpenClaw agent failed")
        send_or_edit_message(
            token,
            chat_id,
            status_message_id,
            format_error("OpenClaw agent failed to evaluate the lot", exc),
            message_id,
        )
        return

    safe_edit_message_text(token, chat_id, status_message_id, check_progress_text("finalizing"))

    if should_send_agent_reply(agent_result):
        reply_text = agent_reply_text(agent_result)
        if reply_text:
            send_or_edit_message(token, chat_id, status_message_id, reply_text, message_id)
        else:
            session_id = agent_result.get("_openclaw_session_id") or "unknown"
            transcript_path = agent_result.get("_openclaw_transcript_path") or "unknown"
            logging.warning("OpenClaw returned no visible text: session_id=%s transcript=%s", session_id, transcript_path)
            send_or_edit_message(
                token,
                chat_id,
                status_message_id,
                "OpenClaw finished without visible reply text. "
                f"session_id={session_id}",
                message_id,
            )
    else:
        send_or_edit_message(token, chat_id, status_message_id, check_progress_text("delivered"), message_id)


def handle_callback_query(token, callback_query):
    callback_query_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not data.startswith(LANGUAGE_CALLBACK_PREFIX):
        if callback_query_id:
            answer_callback_query(token, callback_query_id, "Unsupported action.", True)
        return

    language_code = normalize_language(data.removeprefix(LANGUAGE_CALLBACK_PREFIX))
    if not language_code or chat_id is None:
        if callback_query_id:
            answer_callback_query(token, callback_query_id, "Could not update language.", True)
        return

    set_chat_language(chat_id, language_code)
    safe_edit_message_text(
        token,
        chat_id,
        message_id,
        settings_text(chat_id),
        language_keyboard(language_code),
    )

    if callback_query_id:
        answer_callback_query(token, callback_query_id, f"Language set to {language_label(language_code)}.")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )
    load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    set_bot_commands(token)

    offset = None
    logging.info("Telegram bot started")

    while True:
        payload = {
            "timeout": 30,
            "allowed_updates": json.dumps(["message", "callback_query"]),
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

            callback_query = update.get("callback_query")
            if callback_query:
                try:
                    handle_callback_query(token, callback_query)
                except Exception:
                    logging.exception("Failed to handle Telegram callback query")


if __name__ == "__main__":
    main()
