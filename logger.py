"""EVE World 独立程序日志"""

import logging
import sys

from config import LOG_LEVEL

_LEVEL = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

logging.basicConfig(
    level=_LEVEL,
    format="%(asctime)s [%(levelname)s] eve_world: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("eve_world")
logger.success = lambda msg, *a, **k: logger.info(msg, *a, **k)