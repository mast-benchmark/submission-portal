"""Size limits. Module attributes so tests and the Space can override them."""

MAX_COMPRESSED_BYTES = 200 * 1024 * 1024        # per uploaded file (on disk / in zip)
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # per file after gzip/zip inflation
MAX_LINE_BYTES = 64 * 1024 * 1024                # a single record larger than this is rejected
