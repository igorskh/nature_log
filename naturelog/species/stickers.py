"""Bird sticker illustrations from huashu-bird-sticker, fetched on demand and cached locally.

Stickers: https://github.com/alchaincyf/huashu-bird-sticker (CC BY 4.0).
The index (``data/stickers.json``) and each sticker are downloaded once from
GitHub and kept in the cache folder, so they stay available offline.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

# Raw GitHub, not the jsDelivr CDN the index names: the repository exceeds
# jsDelivr's 50 MB package limit, so it refuses files it has not cached yet.
BASE_URL = "https://raw.githubusercontent.com/alchaincyf/huashu-bird-sticker/main/"
INDEX_URL = BASE_URL + "data/stickers.json"
CREDIT = "Bird stickers: huashu-bird-sticker by Huashu, CC BY 4.0"
CREDIT_URL = "https://github.com/alchaincyf/huashu-bird-sticker"
_TIMEOUT = 20


def _key(name: str) -> str:
    return " ".join(name.replace("_", " ").split()).casefold()


def _download(url: str, target: Path) -> None:
    """Download atomically, so an interrupted fetch never leaves a broken cache file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "NatureLog"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        tmp.write_bytes(response.read())
    os.replace(tmp, target)


class StickerStore:
    """Looks up and caches the sticker of a bird species by scientific or English name."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._files: dict[str, str] | None = None

    def _index(self) -> dict[str, str]:
        """``{normalised name: relative sticker file}``; downloads the index on first use."""
        with self._lock:
            if self._files is None:
                path = self.cache_dir / "stickers.json"
                if not path.exists():
                    _download(INDEX_URL, path)
                data = json.loads(path.read_text(encoding="utf-8"))
                files: dict[str, str] = {}
                for s in data.get("stickers", []):
                    # English names only fill gaps: scientific names win on clashes.
                    if s.get("en"):
                        files.setdefault(_key(s["en"]), s["file"])
                for s in data.get("stickers", []):
                    files[_key(s["scientificName"])] = s["file"]
                self._files = files
            return self._files

    def _file(self, scientific_name: str, common_name: str = "") -> str | None:
        files = self._index()
        return files.get(_key(scientific_name)) or (
            files.get(_key(common_name)) if common_name else None
        )

    def fetch(self, scientific_name: str, common_name: str = "") -> Path | None:
        """The local sticker, downloaded if needed; ``None`` if the species has none.

        Raises ``OSError`` if the download fails.
        """
        file = self._file(scientific_name, common_name)
        if file is None:
            return None
        path = self.cache_dir / file
        if not path.exists():
            _download(BASE_URL + file, path)
        return path
