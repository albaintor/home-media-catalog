"""Bounded extraction of album covers embedded in Kodi SMB/NFS audio files.

Only catalogue-owned references are accepted. A matching saved Network
connection supplies access credentials; no arbitrary user path can be read.
The extraction uses the existing seekable RangeSource and FFmpeg bridge and
never downloads the complete album into Python memory.
"""

from __future__ import annotations

import logging
import subprocess
from urllib.parse import urlsplit

from .local_stream import _registered_media_location
from .playback_browser import _credentials
from .playback_media import LoopbackBridge, network_source

LOG = logging.getLogger(__name__)
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def extract_embedded_cover(request, candidate: str) -> bytes | None:
    if not candidate.startswith("music@"):
        return None
    path = candidate.removeprefix("music@")
    parsed = urlsplit(path)
    if (
        parsed.scheme not in {"smb", "nfs", "ftp", "sftp"}
        or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        return None
    # A server-side artwork mount is an equally valid configured access.
    # It is confined to the configured local root by Artwork._mounted.
    try:
        mounted = request.app.state.artwork._mounted(path)
    except (OSError, ValueError):
        mounted = None

    if mounted is not None:
        origin = str(mounted)
        bridge = None
    else:
        # Otherwise use the saved Network connection, its encrypted password,
        # and the same byte-range bridge as server-side video playback.
        matched = _registered_media_location(request, path)
        if matched is None:
            return None
        location, relative = matched
        store = request.app.state.store
        password = _credentials(
            store, request.app.state.playback_oauth, location["id"]
        )
        source = network_source(
            location, relative, password,
            request.app.state.playback_nfs_mounts,
        )
        bridge = LoopbackBridge(source, name="album-audio")

    def extract(origin: str) -> bytes | None:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", origin,
                    "-map", "0:v:0", "-frames:v", "1",
                    "-vf", "scale='min(800,iw)':-2",
                    "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            LOG.debug("Embedded album art extraction unavailable (%s)", parsed.scheme)
            return None
        if (
            result.returncode != 0
            or not result.stdout
            or len(result.stdout) > _MAX_IMAGE_BYTES
            or not result.stdout.startswith(b"\\xff\\xd8\\xff")
        ):
            return None
        return result.stdout

    if bridge is None:
        return extract(origin)
    with bridge:
        return extract(bridge.url)
