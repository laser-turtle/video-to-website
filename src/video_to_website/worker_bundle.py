"""Create a reproducible, standard-library-only helper from the installed release."""

from __future__ import annotations

import hashlib
import io
import zipfile
from functools import lru_cache
from importlib.resources import files

from .worker_protocol import PROTOCOL
from .worker_setup import SETUP_GUIDES

# An allowlist avoids bundling server code, site data, credentials or caches.
MODULES = (
    "__init__.py", "compute.py", "media.py", "util.py", "whisper.py",
    "work_progress.py", "worker.py", "worker_protocol.py", "worker_setup.py",
)
ENTRY_POINT = '''import sys
if sys.version_info < (3, 11):
    sys.stderr.write("This helper needs Python 3.11 or newer. Install it from https://www.python.org/downloads/\\n")
    raise SystemExit(2)
from video_to_website.worker import main
raise SystemExit(main())
'''


@lru_cache(maxsize=1)
def bundle():
    output = io.BytesIO()
    # Stable member metadata means the same installed source yields the same
    # filename/hash, independently of host paths, file mtimes and request time.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        sources = {"__main__.py": ENTRY_POINT.encode("utf-8")}
        root = files("video_to_website")
        sources.update({"video_to_website/" + name: root.joinpath(name).read_bytes() for name in MODULES})
        for name, content in sorted(sources.items()):
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            archive.writestr(member, content)
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    return payload, digest


def bundle_info():
    payload, digest = bundle()
    return {"filename": "v2w-worker-" + digest[:12] + ".pyz", "sha256": digest,
            "bytes": len(payload), "python_min": "3.11", "protocol": PROTOCOL,
            "guides": SETUP_GUIDES}
