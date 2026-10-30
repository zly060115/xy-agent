"""Tee stdin to console + web.log (for start.bat piping)."""
import sys
from datetime import datetime

LOG = "web.log"


def main() -> None:
    f = open(LOG, "a", encoding="utf-8", errors="replace")
    try:
        for line in sys.stdin:
            ts = datetime.now().strftime("%H:%M:%S")
            stamped = f"[{ts}] {line}"
            try:
                sys.stdout.write(stamped)
                sys.stdout.flush()
            except Exception:
                pass  # console encoding issues must never kill the logger
            f.write(stamped)
            f.flush()
    finally:
        f.close()


if __name__ == "__main__":
    main()
