"""Home Control's health feed: the Fusion kit's own code (fusionkit/health.py, schema
fusion-health/1, docs/HEALTH_FEED.md), kept here under its old name for existing imports.
"""

from .fusionkit.health import (MAX_LABEL, MAX_SUMMARY, MAX_VALUE, MAX_VALUES, SCHEMA, STATUSES,  # noqa: F401
                               check_feed, iso, make_feed, worst)
