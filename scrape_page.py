import argparse
import json
import logging
import os
import sys
from pathlib import Path

from ricardo_parser import DEFAULT_OUTPUT_DIR, DEFAULT_RAW_DIR, fetch_ricardo_lot
from settings import get_proxy_from_env, load_env_file


def parse_args():
    load_env_file()

    parser = argparse.ArgumentParser(description="Parse Ricardo lot data for OpenClaw analysis.")
    parser.add_argument("--url", default=os.getenv("RICARDO_URL"))
    parser.add_argument("--proxy", default=get_proxy_from_env())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    if not args.url:
        parser.error("--url is required or RICARDO_URL must be set")

    return args


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )
    args = parse_args()
    _, payload, _ = fetch_ricardo_lot(
        args.url,
        proxy=args.proxy,
        output_dir=args.output_dir,
        raw_dir=args.raw_dir,
        headless=args.headless,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Parser failed: %s", exc)
        sys.exit(1)
