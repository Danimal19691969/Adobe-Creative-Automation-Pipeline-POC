"""Package init — loads `.env` from the project root, authoritatively.

`.env` is the source of truth for PROVIDER / MODEL / API keys. Two
defaults from python-dotenv that we deliberately override:

  - **Pin the path.** `load_dotenv()` with no argument walks up from the
    current working directory. When `adk web`, pytest, or any tool is
    launched from outside the repo root, the search misses our `.env`
    entirely and the process silently falls through to in-code defaults.
    We pin to ``<package>/../.env`` so the file is found regardless of cwd.
  - **`override=True`.** By default dotenv WON'T replace values already
    in the environment, which lets a stale shell-exported `PROVIDER` or
    `MODEL` override the file the user is editing. We flip the default
    so `.env` is authoritative — if you change it in the file, that's
    what runs.
"""

from pathlib import Path

from dotenv import load_dotenv

_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_DOTENV_PATH, override=True)
