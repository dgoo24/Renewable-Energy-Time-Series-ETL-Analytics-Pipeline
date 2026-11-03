"""Build a portable dashboard for GitHub Pages.

The regular dashboard links to ``output/plots`` and ``output/csv``.  GitHub
Pages cannot see those ignored folders, so this script writes a single HTML
file with the plots embedded as base64 data URIs and without CSV links.

Usage:
    python scripts/build_dashboard_site.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_settings  # noqa: E402
from src.dashboard import generate_dashboard  # noqa: E402

SITE_DIR = PROJECT_ROOT / "site"


def main() -> int:
    settings = load_settings()
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    path = generate_dashboard(
        settings,
        embed_images=True,
        include_csv_links=False,
        path=SITE_DIR / "index.html",
    )
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"Standalone dashboard: {path} ({size_mb:.2f} MiB)")
    print("All displayed data is synthetic and not from a real PV installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
