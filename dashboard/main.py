import argparse
from pathlib import Path

from nicegui import ui

from app.startup import App, validate_run_dir


def _port(s: str) -> int:
    try:
        v = int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError("port must be an integer") from e
    if not (1 <= v <= 65535):
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return v


def parse_args():
    ap = argparse.ArgumentParser(
        description='Browse annotated execution paths with an interactive web interface',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument('run_dir', type=validate_run_dir, help='Directory containing the output of a run')
    ap.add_argument('--port', type=_port, default=8080, help='Port to run the web server on')
    ap.add_argument('--host', default='127.0.0.1', help='Host/interface to bind the web server to')
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    App(run_dir=args.run_dir)
    ui.run(title='Annotated Paths Browser', port=args.port, host=args.host)


if __name__ in {"__main__", "__mp_main__"}:
    main()
