"""URL constants for the FPL public API.

``https://fantasy.premierleague.com/help/rules`` is a client-rendered JS SPA
with no server-side rules content, so it is deliberately not among these
constants — see ``app/rules`` for the rationale and the structured-API-first
strategy that replaces HTML scraping.
"""

BASE_URL = "https://fantasy.premierleague.com/api"
BOOTSTRAP_STATIC_URL = f"{BASE_URL}/bootstrap-static/"
FIXTURES_URL = f"{BASE_URL}/fixtures/"
EVENT_STATUS_URL = f"{BASE_URL}/event-status/"
