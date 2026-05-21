import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote


ROOT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT_DIR / "data" / "bot_settings.json"
DEFAULT_RESPONSE_LANGUAGE = "en"
MAX_FIND_HISTORY_ENTRIES = 100

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


def get_proxy_from_env():
    proxy = os.getenv("RICARDO_PROXY") or os.getenv("OPENCLAW_RICARDO_PROXY")
    if proxy:
        return proxy

    server = os.getenv("OPENCLAW_RICARDO_PROXY_SERVER")
    username = os.getenv("OPENCLAW_RICARDO_PROXY_USERNAME")
    password = os.getenv("OPENCLAW_RICARDO_PROXY_PASSWORD")

    if not server:
        return None

    scheme = "http"
    address = server
    if "://" in server:
        scheme, address = server.split("://", 1)

    if username and password and "@" not in address:
        username = quote(username, safe="")
        password = quote(password, safe="")
        return f"{scheme}://{username}:{password}@{address}"

    return f"{scheme}://{address}"


def normalize_language(value):
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
    return LANGUAGE_ALIASES.get(cleaned)


def language_label(code):
    return LANGUAGE_LABELS.get(code, LANGUAGE_LABELS[DEFAULT_RESPONSE_LANGUAGE])


def supported_language_text():
    return ", ".join(f"{code}={label}" for code, label in LANGUAGE_LABELS.items())


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


def get_chat_recent_context(chat_id):
    settings = load_bot_settings()
    chat_settings = (settings.get("chats") or {}).get(str(chat_id)) or {}
    context = chat_settings.get("recent_context")
    return context if isinstance(context, dict) else None


def set_chat_recent_context(chat_id, context):
    settings = load_bot_settings()
    chats = settings.setdefault("chats", {})
    chat_settings = chats.setdefault(str(chat_id), {})
    chat_settings["recent_context"] = context
    save_bot_settings(settings)


def get_chat_find_history(chat_id):
    settings = load_bot_settings()
    chat_settings = (settings.get("chats") or {}).get(str(chat_id)) or {}
    history = chat_settings.get("find_history")
    return history if isinstance(history, list) else []


def set_chat_find_history(chat_id, history):
    history = history if isinstance(history, list) else []
    settings = load_bot_settings()
    chats = settings.setdefault("chats", {})
    chat_settings = chats.setdefault(str(chat_id), {})
    chat_settings["find_history"] = history[-MAX_FIND_HISTORY_ENTRIES:]
    save_bot_settings(settings)
