"""RAT web application.

Run it with::

    python backend/app.py            # http://127.0.0.1:8000

The app serves the static frontend (``frontend/``) and mounts the REST API
(:mod:`rat.api`) under ``/api``.  All runtime state -- ingested repositories,
columnar indexes, the registry and in-flight job state -- lives under
``data/`` next to the project root (override with ``RAT_DATA`` or ``--data``).
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow `python backend/app.py` as well as `python -m backend.app`.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, send_from_directory  # noqa: E402

from rat import __version__  # noqa: E402
from rat.api import create_api  # noqa: E402
from rat.registry import RepoRegistry  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")


def create_app(data_root: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["RAT_DATA"] = os.path.abspath(
        data_root or os.environ.get("RAT_DATA")
        or os.path.join(PROJECT_ROOT, "data"))
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3      # zip uploads
    app.config["JSON_SORT_KEYS"] = False

    registry = RepoRegistry(app.config["RAT_DATA"])
    app.config["RAT_REGISTRY"] = registry
    app.register_blueprint(create_api(registry, static_root=FRONTEND_DIR),
                           url_prefix="/api")

    @app.get("/")
    def index():
        if not os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
            return jsonify({
                "name": "Repository Analysis Tool",
                "version": __version__,
                "api": "/api/health",
                "note": "the frontend has not been built yet",
            })
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/<path:filename>")
    def static_or_spa(filename: str):
        if filename.startswith("api/"):
            return jsonify({"error": "unknown endpoint"}), 404
        target = os.path.join(FRONTEND_DIR, filename)
        if os.path.isfile(target):
            return send_from_directory(FRONTEND_DIR, filename)
        # unknown non-API path: hand it to the SPA (hash routing handles the rest)
        if os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
            return send_from_directory(FRONTEND_DIR, "index.html")
        return jsonify({"error": "not found"}), 404

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Repository Analysis Tool")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None,
                        help="runtime data directory (default: ./data)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app = create_app(args.data)
    print(f"RAT {__version__}")
    print(f"  data     : {app.config['RAT_DATA']}")
    print(f"  frontend : {FRONTEND_DIR}")
    print(f"  serving  : http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True,
            use_reloader=False)


if __name__ == "__main__":
    main()
