"""Shared test setup.

Importing `app.main` constructs an `Anthropic()` client at module load, which
requires an API key. The read-endpoint tests never call `/chat`, so a dummy key
is enough to let the import succeed without a real credential or network call.
Set before any test module imports `app.main`.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
