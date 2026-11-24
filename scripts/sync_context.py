"""Generate an AGENTS.md from a directory of context/steering markdown files.

This is a thin CLI wrapper around :mod:`lark_agent_bridge.context_sync` (the
canonical implementation, also used by the bot's refresh-on-change).

Usage:
    python scripts/sync_context.py --src .kiro/steering --out AGENTS.md

Run it whenever the source files change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lark_agent_bridge.context_sync import build, write_atomic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate AGENTS.md from a markdown context directory."
    )
    parser.add_argument("--src", default=".kiro/steering", help="source directory of *.md files")
    parser.add_argument("--out", default="AGENTS.md", help="output file path")
    parser.add_argument("--order", nargs="*", default=[],
                        help="optional ordered filenames to place first")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        parser.error(f"source directory not found: {src}")
    out = Path(args.out)

    content, files = build(src, args.order)
    write_atomic(out, content)
    print(f"Wrote {out} ({out.stat().st_size} bytes) from {len(files)} files:")
    for f in files:
        print(f"  - {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
