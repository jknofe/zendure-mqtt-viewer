"""Read-only ASCII dashboard for a Zendure SolarFlow hub over MQTT."""

import logging

# Without a handler, logging.lastResort writes every WARNING and above to
# stderr - which, under curses, means log text painted across the running
# dashboard. A NullHandler on the package logger switches that off for good;
# errorlog.configure() then points the records at a file instead.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["__version__"]
__version__ = "0.1.0"
