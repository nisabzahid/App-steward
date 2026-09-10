import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            },
            ensure_ascii=False,
        )


def configure_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(JsonFormatter())
    root.addHandler(console)

    state = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "installed-software"
    )
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state, 0o700)
        filename = state / "application.jsonl"
        descriptor = os.open(
            filename,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        handler = RotatingFileHandler(
            filename, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    except OSError:
        logging.getLogger(__name__).warning("file_logging_unavailable")


def main():
    if os.geteuid() == 0:
        print(
            "Do not run the GUI as root. Run it as your normal desktop user.",
            file=sys.stderr,
        )
        return 1
    configure_logging()
    from .ui import ManagerApplication

    return ManagerApplication().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
