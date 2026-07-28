"""Entry point: python -m services.telegram_bot"""

import asyncio

from .bot import main

asyncio.run(main())
