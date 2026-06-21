from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("muscat-db")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# Legacy alias kept for any code that imports VERSION directly
VERSION = __version__
