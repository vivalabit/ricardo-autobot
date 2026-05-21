import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib import error, parse, request
from urllib.parse import urlparse

from openclaw_client import (
    agent_reply_text,
    build_agent_message,
    build_find_agent_message,
    build_question_agent_message,
    build_question_payload,
    run_openclaw_agent,
    should_send_agent_reply,
)
from settings import (
    LANGUAGE_LABELS,
    default_response_language,
    get_chat_find_history,
    get_chat_recent_context,
    get_chat_language,
    language_label,
    load_env_file,
    normalize_language,
    set_chat_recent_context,
    set_chat_find_history,
    set_chat_language,
    supported_language_text,
)


RICARDO_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
CHECK_COMMAND_RE = re.compile(r"^\s*/check(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
FIND_COMMAND_RE = re.compile(r"^\s*/find(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
QUESTION_COMMAND_RE = re.compile(r"^\s*/question(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
FIND_BUDGET_RANGE_RE = re.compile(
    r"(?P<low>\d[\d'’.,]*)\s*(?:-|–|—|to|bis|до)\s*"
    r"(?P<high>\d[\d'’.,]*)"
    r"(?:\s*(?P<currency>chf|sfr|francs?|franken|frs?\.?|франк(?:ов|а|и)?|\.-))?",
    re.IGNORECASE,
)
FIND_BUDGET_RE = re.compile(
    r"(?P<marker>(?:до|макс(?:имум)?|max(?:imum)?|under|up\s+to|<=|bis|unter|for|für|pour|jusqu[’']?a|jusqu[’']?à|moins\s+de)\s+)?"
    r"(?P<currency_before>(?:chf|sfr|francs?|franken|frs?\.?|франк(?:ов|а|и)?)\s+)?"
    r"(?P<amount>(?:\d{1,3}(?:[\s'’.,]\d{3})+|\d+(?:[.,]\d+)?))"
    r"(?:\s*(?P<currency_after>chf|sfr|francs?|franken|frs?\.?|франк(?:ов|а|и)?|\.-))?",
    re.IGNORECASE,
)
FIND_DELIVERY_ONLY_RE = re.compile(
    r"(?:"
    r"\b(?:only\s+(?:shipping|delivery)|(?:shipping|delivery)\s+only|with\s+(?:shipping|delivery))\b"
    r"|\b(?:nur\s+versand|mit\s+versand)\b"
    r"|(?:только\s+(?:с\s+)?доставк\w*|с\s+доставк\w*)"
    r")",
    re.IGNORECASE,
)
FIND_UNIQUE_FLAGS = {"-u", "--unique"}
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


def extract_question_argument(text):
    match = QUESTION_COMMAND_RE.match(text or "")
    return match.group(1).strip() if match and match.group(1) else ""


def is_question_command(text):
    return bool(re.match(r"^\s*/question(?:@\w+)?(?:\s|$)", text or "", re.IGNORECASE))


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


def extract_find_unique_flag(argument):
    parts = re.split(r"\s+", (argument or "").strip())
    if not parts or parts == [""]:
        return "", False

    unique_only = False
    remaining = []
    for part in parts:
        if part.lower() in FIND_UNIQUE_FLAGS and not unique_only:
            unique_only = True
            continue
        remaining.append(part)

    return " ".join(remaining).strip(), unique_only


def attach_find_flags(request, *, unique_only=False):
    if unique_only:
        request["unique_only"] = True
    return request


def parse_find_argument(argument):
    cleaned, unique_only = extract_find_unique_flag(argument)
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None

    delivery_only = bool(FIND_DELIVERY_ONLY_RE.search(cleaned))
    if delivery_only:
        cleaned = FIND_DELIVERY_ONLY_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
        if not cleaned:
            return None

    budget_range_match = None
    for match in FIND_BUDGET_RANGE_RE.finditer(cleaned):
        low_budget = parse_budget_amount(match.group("low"))
        high_budget = parse_budget_amount(match.group("high"))
        if not low_budget or not high_budget or high_budget < low_budget or high_budget < 50:
            continue
        budget_range_match = match

    if budget_range_match:
        min_price = parse_budget_amount(budget_range_match.group("low"))
        max_price = parse_budget_amount(budget_range_match.group("high"))
        item_query = f"{cleaned[:budget_range_match.start()]} {cleaned[budget_range_match.end():]}".strip(" ,;:-")
        item_query = re.sub(r"\s+", " ", item_query).strip()
        if item_query:
            request = {
                "item_query": item_query,
                "budget_chf": max_price,
                "min_price_chf": min_price,
                "max_price_chf": max_price,
            }
            if delivery_only:
                request["delivery_only"] = True
            return attach_find_flags(request, unique_only=unique_only)

    budget_match = None
    for match in FIND_BUDGET_RE.finditer(cleaned):
        budget = parse_budget_amount(match.group("amount"))
        if not budget:
            continue
        has_budget_signal = bool(match.group("marker") or match.group("currency_before") or match.group("currency_after"))
        if not has_budget_signal and budget > 2000:
            continue
        budget_match = match

    if budget_match:
        budget = parse_budget_amount(budget_match.group("amount"))
        item_query = f"{cleaned[:budget_match.start()]} {cleaned[budget_match.end():]}".strip(" ,;:-")
        item_query = re.sub(r"\s+", " ", item_query).strip()

        if item_query:
            request = {
                "item_query": item_query,
                "budget_chf": budget,
                "min_price_chf": None,
                "max_price_chf": budget,
            }
            if delivery_only:
                request["delivery_only"] = True
            return attach_find_flags(request, unique_only=unique_only)

    item_query = cleaned

    request = {
        "item_query": item_query,
        "budget_chf": None,
        "min_price_chf": None,
        "max_price_chf": None,
    }
    if delivery_only:
        request["delivery_only"] = True
    return attach_find_flags(request, unique_only=unique_only)


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
        {"command": "question", "description": "Ask about the recent lot, search, or anything else"},
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


def run_search_parser(item_query, max_price_chf=None, min_price_chf=None, delivery_only=False):
    from ricardo_parser import parse_ricardo_search

    return parse_ricardo_search(
        item_query,
        max_price_chf,
        min_price_chf=min_price_chf,
        delivery_only=delivery_only,
        headless=True,
    )


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
            "/find <item> [price_or_range] [только доставка]",
            "/find -u <item> [price_or_range] - only new listings not seen in previous searches",
            "/question <question>",
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
            "Find items with /find <item> [price_or_range], for example /find видеокарту до 500 франков.",
            "Use /find -u <item> to hide listings already shown in previous searches.",
            "Ask follow-up questions with /question <question>.",
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
            "/find <item> [price_or_range]",
            "/find -u <item> [price_or_range]  (only new listings)",
            "",
            "Examples:",
            "/find Видеокарта для игр",
            "/find -u Видеокарта для игр",
            "/find видеокарту до 500 франков",
            "/find Видеокарта для игр 350-500 франков",
            "/find dyson только доставка",
            "/find RTX 4070 500 CHF",
        ]
    )


def question_usage_text():
    return "\n".join(
        [
            "Use:",
            "/question <question>",
            "",
            "Examples:",
            "/question Is this lot worth bidding on up to 200 CHF?",
            "/question Which found option has the lowest risk?",
            "/question What should I check before buying used electronics?",
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


def question_progress_text(step):
    messages = {
        "answering": "Answering question...\n\n[1/2] Preparing recent context.",
        "finalizing": "Answering question...\n\n[2/2] Preparing the Telegram answer.",
        "delivered": "Question answered. OpenClaw sent the answer to this chat.",
    }
    return messages[step]


def remember_chat_context(chat_id, kind, user_text, payload):
    context = {
        "kind": kind,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "telegram_message": user_text,
        "payload": payload,
    }
    try:
        set_chat_recent_context(chat_id, context)
    except Exception:
        logging.warning("Failed to save recent chat context: chat_id=%s kind=%s", chat_id, kind, exc_info=True)


def find_candidate_refs(candidate):
    if not isinstance(candidate, dict):
        return None, None

    listing_id = str(candidate.get("listing_id") or "").strip() or None
    url = str(candidate.get("url") or "").strip() or None
    return listing_id, url


def find_history_seen_refs(history):
    seen_listing_ids = set()
    seen_urls = set()
    for entry in history or []:
        if not isinstance(entry, dict):
            continue

        for listing_id in entry.get("listing_ids") or []:
            listing_id = str(listing_id or "").strip()
            if listing_id:
                seen_listing_ids.add(listing_id)

        for url in entry.get("urls") or []:
            url = str(url or "").strip()
            if url:
                seen_urls.add(url)

    return seen_listing_ids, seen_urls


def collect_find_candidate_refs(candidates):
    listing_ids = []
    urls = []
    seen_listing_ids = set()
    seen_urls = set()

    for candidate in candidates or []:
        listing_id, url = find_candidate_refs(candidate)
        if listing_id and listing_id not in seen_listing_ids:
            seen_listing_ids.add(listing_id)
            listing_ids.append(listing_id)
        if url and url not in seen_urls:
            seen_urls.add(url)
            urls.append(url)

    return listing_ids, urls


def filter_unique_find_payload(payload, history):
    if not isinstance(payload, dict):
        return payload

    candidates = payload.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    seen_listing_ids, seen_urls = find_history_seen_refs(history)
    filtered_candidates = []
    excluded_candidates = []

    for candidate in candidates:
        listing_id, url = find_candidate_refs(candidate)
        if (listing_id and listing_id in seen_listing_ids) or (url and url in seen_urls):
            excluded_candidates.append(candidate)
            continue
        filtered_candidates.append(candidate)

    rejected = list(payload.get("rejected") or [])
    for candidate in excluded_candidates[:10]:
        listing_id, url = find_candidate_refs(candidate)
        rejected.append(
            {
                "url": url,
                "listing_id": listing_id,
                "reason": "already_seen_in_previous_searches",
            }
        )

    search = dict(payload.get("search") or {})
    search["unique_only"] = True
    search["previous_search_count"] = len(history or [])
    search["pre_unique_result_count"] = len(candidates)
    search["excluded_previous_result_count"] = len(excluded_candidates)
    search["result_count"] = len(filtered_candidates)

    return {
        **payload,
        "search": search,
        "candidates": filtered_candidates,
        "rejected": rejected,
    }


def remember_find_history(chat_id, item_query, user_text, payload):
    candidates = payload.get("candidates") if isinstance(payload, dict) else []
    listing_ids, urls = collect_find_candidate_refs(candidates)
    if not listing_ids and not urls:
        return

    entry = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "query": item_query,
        "telegram_message": user_text,
        "listing_ids": listing_ids,
        "urls": urls,
    }

    try:
        history = get_chat_find_history(chat_id)
        history.append(entry)
        set_chat_find_history(chat_id, history)
    except Exception:
        logging.warning("Failed to save find history: chat_id=%s query=%s", chat_id, item_query, exc_info=True)


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

    if is_question_command(text):
        question = extract_question_argument(text)
        if not question:
            send_message(token, chat_id, question_usage_text(), message_id)
            return

        send_chat_action(token, chat_id)
        progress_message = send_message(token, chat_id, question_progress_text("answering"), message_id)
        status_message_id = progress_message_id(progress_message)
        recent_context = get_chat_recent_context(chat_id)
        response_language = get_chat_language(chat_id)
        question_payload = build_question_payload(question, text, response_language, recent_context)

        try:
            agent_result = run_openclaw_agent(
                build_question_agent_message(question, text, response_language, recent_context, question_payload),
                chat_id=chat_id,
                message_id=message_id,
                payload=question_payload,
            )
        except Exception as exc:
            logging.exception("OpenClaw agent failed during question answering")
            send_or_edit_message(
                token,
                chat_id,
                status_message_id,
                format_error("OpenClaw agent failed to answer the question", exc),
                message_id,
            )
            return

        safe_edit_message_text(token, chat_id, status_message_id, question_progress_text("finalizing"))

        if should_send_agent_reply(agent_result):
            reply_text = agent_reply_text(agent_result)
            if reply_text:
                send_or_edit_message(token, chat_id, status_message_id, reply_text, message_id)
            else:
                session_id = agent_result.get("_openclaw_session_id") or "unknown"
                transcript_path = agent_result.get("_openclaw_transcript_path") or "unknown"
                logging.warning(
                    "OpenClaw returned no visible question text: session_id=%s transcript=%s",
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
            send_or_edit_message(token, chat_id, status_message_id, question_progress_text("delivered"), message_id)

        return

    if is_find_command(text):
        find_request = parse_find_argument(extract_find_argument(text))
        if not find_request:
            send_message(token, chat_id, find_usage_text(), message_id)
            return

        item_query = find_request["item_query"]
        budget_chf = find_request.get("budget_chf")
        min_price_chf = find_request.get("min_price_chf")
        max_price_chf = find_request.get("max_price_chf")
        delivery_only = bool(find_request.get("delivery_only"))
        unique_only = bool(find_request.get("unique_only"))

        send_chat_action(token, chat_id)
        progress_message = send_message(token, chat_id, find_progress_text("searching"), message_id)
        status_message_id = progress_message_id(progress_message)

        try:
            payload = run_search_parser(item_query, max_price_chf, min_price_chf, delivery_only)
        except Exception as exc:
            logging.exception(
                "Failed to parse Ricardo search: query=%s min_price=%s max_price=%s delivery_only=%s",
                item_query,
                min_price_chf,
                max_price_chf,
                delivery_only,
            )
            send_or_edit_message(
                token,
                chat_id,
                status_message_id,
                format_error("Failed to search live Ricardo listings", exc),
                message_id,
            )
            return

        if unique_only:
            payload = filter_unique_find_payload(payload, get_chat_find_history(chat_id))

        remember_find_history(chat_id, item_query, text, payload)
        remember_chat_context(chat_id, "search", text, payload)

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

    remember_chat_context(chat_id, "lot", text, payload)

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
