"""Single source of truth for the product version.

Kept in its own tiny module so lightweight components (the desktop GUI/Control
Panel) can read the version without importing the full application graph (which
pulls in botbuilder, the AI provider, etc.). ``app.py`` re-exports this and
surfaces it in ``/health``; keep it in step with the installer ``-Version`` at
release time.
"""

__version__ = "2.0.1"
