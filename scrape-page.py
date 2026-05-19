import logging
import sys

from scrape_page import main


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Parser failed: %s", exc)
        sys.exit(1)
