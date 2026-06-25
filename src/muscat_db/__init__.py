from importlib.metadata import version, PackageNotFoundError

# Load .env before any submodule reads os.environ (some read at import time).
# Searches upward from CWD so it works from any subdirectory. See config.py for
# the canonical registry of variables and .env.example for the template.
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

try:
    __version__ = version("muscat-db")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# Legacy alias kept for any code that imports VERSION directly
VERSION = __version__

__muscatdb_version__ = __version__

__meta__ = {
    "__muscatdb_version__": __muscatdb_version__,
    "__version__": __version__,
    "package": "muscat-db",
}
