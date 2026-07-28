"""Entry point: python -m services.corporate_memory"""

import sys

from .collector import main

sys.exit(main())
