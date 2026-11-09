import json
import re
import unicodedata
from typing import Dict, Iterable, List, Union

from ovos_utils import classproperty
from ovos_utils.log import LOG
from ovos_bus_client.message import Message
from ovos_utils.ocp import MediaEntry, MediaType, PlaybackType, PlayerState, Playlist
from ovos_utils.parse import MatchStrategy, fuzzy_match
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import ocp_search
from ovos_workshop.skills.common_play import OVOSCommonPlaybackSkill

from ovos_skill_plex.plex_client import PlexMusicClient, PlexResult

DEFAULT_HOST = "http://127.0.0.1:32400"  # real URL comes from settings.json


class PlexMusicSkill(OVOSCommonPlaybackSkill):
    def __init__(self, *args, **kwargs):
        self._client = None
        self._client_key = None
        self._served_results = False  # our results fed the latest OCP search
        self._categories: Dict[str, dict] = {}  # lang -> categories.json table
        super().__init__(supported_media=[MediaType.MUSIC, MediaType.GENERIC],
                         skill_voc_filename="plex_skill",
                         *args, **kwargs)

    def initialize(self):
        self.add_event("ovos.common_play.player.state", self.handle_player_state)

    def handle_player_state(self, message):
        if message.data.get("state") == PlayerState.STOPPED:
            self._served_results = False

    # The stop pipeline sends "para"/"stop" to the last active skill, which is
    # this one after OCP plays our results; forward it to the OCP player.
    def can_stop(self, message=None) -> bool:
        return self._served_results

    def stop(self):
        if self.can_stop():
            LOG.info("Stop requested, stopping OCP playback")
            self.bus.emit(Message("ovos.common_play.stop"))
            self._served_results = False

    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(internet_before_load=False,
                                   network_before_load=True,
                                   gui_before_load=False,
                                   requires_internet=False,
                                   requires_network=True,
                                   requires_gui=False,
                                   no_internet_fallback=True,
                                   no_network_fallback=False,
                                   no_gui_fallback=True)

    @property
    def client(self):
        """Plex client for the current settings; rebuilt if host/token change."""
        host = self.settings.get("host") or DEFAULT_HOST
        token = self.settings.get("token") or ""
        if not token:
            return None
        if self._client is None or self._client_key != (host, token):
            self._client = PlexMusicClient(host, token,
                                           max_tracks=self.settings.get("max_tracks", 50))
            self._client_key = (host, token)
        return self._client

    def strip_filler(self, phrase: str) -> str:
        """Drop words like "música de" / "el álbum" that Plex search can't match."""
        fillers = sorted(self.voc_list("filler"), key=len, reverse=True)
        for f in fillers:
            phrase = re.sub(rf"(^|\s){re.escape(f)}(?=\s|$)", " ", phrase, flags=re.IGNORECASE)
        words = phrase.split()
        if words and self.voc_match(words[0], "leading_preposition", exact=True):
            words = words[1:]  # "pon música de X" can reach us as "de X"
        return " ".join(words)

    def calc_score(self, phrase: str, result: PlexResult, base_score: int) -> int:
        phrase = phrase.lower()
        title_score = fuzzy_match(phrase, result.title.lower(),
                                  strategy=MatchStrategy.TOKEN_SET_RATIO)
        full_score = fuzzy_match(phrase, f"{result.artist} {result.title}".lower(),
                                 strategy=MatchStrategy.TOKEN_SET_RATIO)
        return min(100, int(base_score + 70 * max(title_score, full_score)))

    def _to_ocp(self, result: PlexResult, score: int) -> Union[MediaEntry, Playlist]:
        entries = [MediaEntry(uri=t.uri,
                              title=t.title,
                              artist=t.artist,
                              image=t.image,
                              length=t.length_ms,
                              match_confidence=score,
                              playback=PlaybackType.AUDIO,
                              media_type=MediaType.MUSIC,
                              skill_id=self.skill_id)
                   for t in result.tracks]
        if result.kind == "track":
            return entries[0]
        pl = Playlist(title=result.title,
                      artist=result.artist,
                      image=result.image,
                      match_confidence=score,
                      playback=PlaybackType.AUDIO,
                      media_type=MediaType.MUSIC,
                      skill_id=self.skill_id)
        for e in entries:
            pl.append(e)
        return pl

    def categories(self) -> Dict[str, Dict[str, List[str]]]:
        """Spoken term -> {facet kind: [English keywords]} from locale/<lang>/categories.json."""
        if self.lang not in self._categories:
            path = self.find_resource("categories.json", lang=self.lang)
            table = {}
            if path:
                with open(path, encoding="utf-8") as f:
                    table = {_norm(k): v for k, v in json.load(f).items()}
            self._categories[self.lang] = table
        return self._categories[self.lang]

    def resolve_category(self, client: PlexMusicClient, phrase: str):
        """Map a phrase like "rock" or "tranquila" to Plex facet filters, or None."""
        facets = client.facet_values()
        spec = self.categories().get(_norm(phrase))
        if spec is None:
            # Unlisted terms ("britpop", "heavy metal") can still name a Plex genre/style.
            if len(phrase) < 4:
                return None
            spec = {kind: [_norm(phrase)] for kind in ("genre", "style")}
        filters = {}
        for kind, keywords in spec.items():
            for fld, values in facets.get(kind, {}).items():
                matched = [v for v in values if any(k in _norm(v) for k in keywords)]
                if matched:
                    filters[fld] = matched
        return filters or None

    def resolve_playlist(self, client: PlexMusicClient, phrase: str, min_score: float):
        best, best_score = None, 0.0
        for title in client.playlist_titles():
            score = fuzzy_match(_norm(phrase), _norm(title))
            if score > best_score:
                best, best_score = title, score
        return best if best_score >= min_score else None

    def _special_results(self, client: PlexMusicClient, phrase: str, base_score: int):
        """Playlist or genre/style/mood matches; these replace the normal name search."""
        if self.voc_match(phrase, "playlist"):
            name = self.remove_voc(phrase, "playlist").strip()
            title = self.resolve_playlist(client, name, min_score=0.6) if name else None
            result = client.playlist(title) if title else None
            return [(result, min(100, base_score + 80))] if result else []
        title = self.resolve_playlist(client, phrase, min_score=0.9)
        if title:
            result = client.playlist(title)
            if result:
                return [(result, min(100, base_score + 75))]
        filters = self.resolve_category(client, phrase)
        if filters:
            tracks = client.tracks_by_facets(filters)
            if tracks:
                result = PlexResult("category", phrase.capitalize(), "", tracks[0].image, tracks)
                return [(result, min(100, base_score + 75))]
        return []

    @ocp_search()
    def search_plex(self, phrase, media_type) -> Iterable[Union[MediaEntry, Playlist]]:
        base_score = 15 if media_type == MediaType.MUSIC else 0
        if self.voc_match(phrase, "plex"):
            base_score += 30
            phrase = self.remove_voc(phrase, "plex").strip()
        phrase = self.strip_filler(phrase)
        if not phrase:
            return
        client = self.client
        if client is None:
            LOG.warning("Plex token not set in skill settings")
            return
        try:
            scored = self._special_results(client, phrase, base_score)
            if not scored:
                scored = [(r, self.calc_score(phrase, r, base_score)) for r in client.search(phrase)]
        except Exception as e:
            LOG.error(f"Plex search failed: {e}")
            client.reset()
            return
        self._served_results = bool(scored)
        for r, score in scored:
            yield self._to_ocp(r, score)


def _norm(text: str) -> str:
    """Lowercase and drop accents so "clásica" == "clasica"."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()
