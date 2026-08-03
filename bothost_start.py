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
from memory_trim import trim_memory


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    os.chdir(Path(__file__).resolve().parent)
    ensure_system_deps()
    trim_memory("bootstrap")
    from bot import main as bot_main

    trim_memory("imports")
    asyncio.run(bot_main())


if __name__ == "__main__":
    main()
