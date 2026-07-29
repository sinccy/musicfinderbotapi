#!/usr/bin/env python3
"""
Bothost entry point.
Bothost запускает: python <главный_файл> — расширение не важно.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from bootstrap import ensure_system_deps


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    os.chdir(Path(__file__).resolve().parent)
    ensure_system_deps()
    from bot import main as bot_main

    asyncio.run(bot_main())


if __name__ == "__main__":
    main()
