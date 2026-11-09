"""Thin wrapper around plexapi: connect lazily and search the music libraries."""
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ovos_utils.log import LOG
from plexapi.server import PlexServer

MUSIC_TYPES = ("artist", "album", "track")
CACHE_SECONDS = 600

# Facet kind -> list of (plex filter field, libtype used to list its values).
# A genre can be tagged on the artist or the album, so both are queried.
FACET_FIELDS = {
    "genre": [("artist.genre", "artist"), ("album.genre", "album")],
    "style": [("album.style", "album")],
    "mood": [("mood", "track")],
}


@dataclass
class PlexTrack:
    title: str
    artist: str
    album: str
    uri: str
    length_ms: int
    image: str


@dataclass
class PlexResult:
    kind: str  # "artist" | "album" | "track"
    title: str
    artist: str
    image: str
    tracks: List[PlexTrack] = field(default_factory=list)


class PlexMusicClient:
    def __init__(self, host: str, token: str, timeout: int = 10, max_tracks: int = 50):
        self.host = host.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_tracks = max_tracks
        self._server: Optional[PlexServer] = None
        self._cache: Dict[str, tuple] = {}  # key -> (timestamp, value)

    @property
    def server(self) -> PlexServer:
        if self._server is None:
            self._server = PlexServer(self.host, self.token, timeout=self.timeout)
            LOG.info(f"Connected to Plex server '{self._server.friendlyName}'")
        return self._server

    def reset(self):
        self._server = None
        self._cache.clear()

    def _cached(self, key, loader):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = loader()
        self._cache[key] = (time.time(), value)
        return value

    @property
    def music_sections(self):
        return self._cached("sections", lambda: [s for s in self.server.library.sections()
                                                 if s.type == "artist"])

    def _image(self, item) -> str:
        thumb = getattr(item, "thumb", None) or getattr(item, "parentThumb", None)
        return self.server.url(thumb, includeToken=True) if thumb else ""

    def _track(self, t) -> Optional[PlexTrack]:
        try:
            part = t.media[0].parts[0]
        except (IndexError, AttributeError):
            return None
        return PlexTrack(
            title=t.title,
            artist=t.originalTitle or t.grandparentTitle or "",
            album=t.parentTitle or "",
            uri=self.server.url(part.key, includeToken=True),
            length_ms=t.duration or 0,
            image=self._image(t),
        )

    def _tracks(self, items) -> List[PlexTrack]:
        tracks = (self._track(t) for t in items[: self.max_tracks])
        return [t for t in tracks if t]

    def search(self, phrase: str, limit: int = 5) -> List[PlexResult]:
        """Search artists, albums and tracks; return them with playable tracks."""
        results = []
        for item in self.server.search(phrase, limit=limit):
            if item.type not in MUSIC_TYPES:
                continue
            if item.type == "artist":
                results.append(PlexResult("artist", item.title, item.title,
                                          self._image(item), self._tracks(item.tracks())))
            elif item.type == "album":
                results.append(PlexResult("album", item.title, item.parentTitle or "",
                                          self._image(item), self._tracks(item.tracks())))
            else:
                track = self._track(item)
                if track:
                    results.append(PlexResult("track", track.title, track.artist,
                                              track.image, [track]))
        return [r for r in results if r.tracks]

    def facet_values(self) -> Dict[str, Dict[str, List[str]]]:
        """{kind: {plex field: [values]}} for genres, styles and moods in the library."""
        def load():
            values = {}
            for kind, fields in FACET_FIELDS.items():
                values[kind] = {}
                for fld, libtype in fields:
                    names = set()
                    for section in self.music_sections:
                        tag = fld.split(".")[-1]
                        names.update(c.title for c in section.listFilterChoices(tag, libtype=libtype))
                    values[kind][fld] = sorted(names)
            return values
        return self._cached("facets", load)

    def tracks_by_facets(self, filters: Dict[str, List[str]]) -> List[PlexTrack]:
        """Random tracks matching ANY of {plex field: [values]}, deduplicated and capped."""
        seen, tracks = set(), []
        for fld, values in filters.items():
            if not values:
                continue
            for section in self.music_sections:
                for t in section.search(libtype="track", sort="random",
                                        maxresults=self.max_tracks, filters={fld: values}):
                    if t.ratingKey not in seen:
                        seen.add(t.ratingKey)
                        tracks.append(t)
        random.shuffle(tracks)
        return self._tracks(tracks)

    def playlist_titles(self) -> List[str]:
        return self._cached("playlists", lambda: [p.title for p in self.server.playlists()
                                                  if p.playlistType == "audio"])

    def playlist(self, title: str) -> Optional[PlexResult]:
        pl = self.server.playlist(title)
        tracks = self._tracks(pl.items())
        return PlexResult("playlist", pl.title, "", self._image(pl), tracks) if tracks else None
