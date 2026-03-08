"""Allow running as: python -m telegram_bot"""

import asyncio

from .bot import main

asyncio.run(main())
