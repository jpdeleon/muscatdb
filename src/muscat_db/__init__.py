from importlib.metadata import version, PackageNotFoundError

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
