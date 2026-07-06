"""Test-wide configuration.

Tests use DATA_DIR paths under PROJECT_ROOT/tmp (inside the project tree),
which is normally rejected by the production config validator to prevent
real customer data from being readable by coding agents.

Set ALLOW_UNSAFE_DATA_DIR=1 for the whole test session so the validator
permits in-tree paths during tests. Production deploys must not set this
variable.
"""

import os


os.environ.setdefault("ALLOW_UNSAFE_DATA_DIR", "1")
