"""Website change monitor: real changes on the pages you watch, as GitHub issues with the
diff and as Slack, Discord or webhook messages.

Pages are read as Markdown through the ScrapeUnblocker getPageSource API:
https://docs.scrapeunblocker.com/guides/markdown?utm_source=github&utm_medium=integration&utm_campaign=example-repos
"""

from .collect import CollectResult, FetchError, collect, fetch_page
from .config import Config, ConfigError, Page, Rules, load_config
from .process import Diff, Event, PriceChange, compare, evaluate, normalize

__all__ = [
    "CollectResult",
    "Config",
    "ConfigError",
    "Diff",
    "Event",
    "FetchError",
    "Page",
    "PriceChange",
    "Rules",
    "collect",
    "compare",
    "evaluate",
    "fetch_page",
    "load_config",
    "normalize",
]

__version__ = "0.1.0"
