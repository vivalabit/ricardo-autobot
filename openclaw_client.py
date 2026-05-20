import json
import os
import re
import shlex
import subprocess
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

from settings import ROOT_DIR, env_flag, env_int, language_label


DEFAULT_OPENCLAW_AGENT_ID = "ricardo-resale"
DEFAULT_OPENCLAW_TIMEOUT_SECONDS = 600
DEFAULT_TRANSCRIPT_WAIT_SECONDS = 12
TRANSCRIPT_POLL_INTERVAL_SECONDS = 0.5
TELEGRAM_MESSAGE_LIMIT = 3900


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


def build_find_payload(item_query, budget_chf, *, candidates=None, rejected=None):
    search_url = f"https://www.ricardo.ch/de/s/{quote(item_query.strip(), safe='')}/"
    return {
        "schema": "openclaw.ricardo.search.v1",
        "source": {
            "provider": "ricardo",
            "url": search_url,
            "parsed_at": date.today().isoformat(),
        },
        "search": {
            "query": item_query,
            "max_budget_chf": budget_chf,
            "search_url": search_url,
            "result_count": len(candidates or []),
        },
        "candidates": candidates or [],
        "rejected": rejected or [],
    }


def build_find_agent_message(item_query, budget_chf, user_text, response_language, payload=None):
    if payload is None:
        payload = build_find_payload(item_query, budget_chf)

    language_name = language_label(response_language)
    search = payload.get("search") or {}
    request_context = {
        "telegram_message": user_text,
        "command": "find",
        "item_query": search.get("query") or item_query,
        "max_budget_chf": search.get("max_budget_chf") or budget_chf,
        "ricardo_search_url": search.get("search_url") or f"https://www.ricardo.ch/de/s/{quote(item_query.strip(), safe='')}/",
        "ricardo_search_urls": search.get("search_urls") or [],
        "query_variants": search.get("query_variants") or [],
        "parser_result_count": search.get("result_count"),
        "parser_scanned_listing_count": search.get("scanned_listing_count"),
        "current_date": date.today().isoformat(),
        "response_language": language_name,
    }

    return "\n".join(
        [
            "Prepare a concise Telegram answer from the provided Ricardo.ch search JSON.",
            "The bot parser already opened Ricardo live search results and candidate listing pages.",
            "For non-German user requests, the parser searched German Ricardo query variants; use the original Telegram message only to understand intent and wording.",
            "Use only candidates from the provided JSON. Do not add links from web search or old indexed pages.",
            f"Return only the final Telegram-ready answer in {language_name}. Do not return JSON.",
            "Select the best 5-8 candidates when available, each with title, price, direct Ricardo.ch URL, and a short reason why it fits.",
            "Prefer gaming-relevant cards for gaming requests, and avoid candidates with risk_flags unless they are the only options.",
            "For auctions, mention that the current price may rise.",
            "If the candidates list is empty, say that no parser-verified active Ricardo listings were found under budget and suggest better search terms.",
            "Do not invent listings, prices, seller details, or URLs.",
            "",
            "Request context:",
            json.dumps(request_context, ensure_ascii=False, indent=2),
            "",
            "Ricardo search JSON:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


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


def last_error_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else None
