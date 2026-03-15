"""
Точка входа: Predict Fun Liquidity v3
"""

import logging


class _NoPredictMakerSigner(logging.Filter):
    def filter(self, record):
        m = (record.getMessage() or "").lower()
        if "maker" in m and "signer" in m and "ignored" in m:
            return False
        return True


_f = _NoPredictMakerSigner()
logging.getLogger("predict_sdk").setLevel(logging.CRITICAL)
logging.getLogger("predict_sdk").addFilter(_f)
logging.getLogger().addFilter(_f)

import warnings
warnings.filterwarnings("ignore", message=".*Predict account.*")
warnings.filterwarnings("ignore", message=".*maker.*signer.*ignored.*")

import builtins
import logger as _logger
builtins.print = _logger.log_print

from gui import main

if __name__ == "__main__":
    main()
