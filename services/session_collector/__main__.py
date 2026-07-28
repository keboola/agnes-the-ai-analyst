"""Entry point: python -m services.session_collector"""

import sys

from .collector import main

sys.exit(main())
