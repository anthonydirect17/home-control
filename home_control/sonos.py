"""Sonos: play, pause, next, previous, volume, mute, source, favorites, and the soundbar's night and speech modes.

Local control only, on the home network, through SoCo (https://github.com/SoCo/SoCo):

- **Finding the speakers:** an SSDP search (UDP to 239.255.255.250:1900). Windows lets the replies
  in as answers to our own search, so no inbound firewall rule is needed. Each speaker is
  remembered by its Sonos id, name and last address in the private settings, and found again by
  searching if its address changes.
- **Talking to them:** plain HTTP from this machine to each speaker (TCP 1400). Outbound only.
- **No event listener:** state is polled every few seconds instead, so the speaker never has to
  connect back to this machine and no port is opened. (Sonos events would need an inbound rule.)

Alert sounds and spoken messages come later: the speaker fetches the sound from this machine,
so that needs the registered LAN port 8781 and its firewall rule, by its own card.
"""

import threading
import time

from .services import PollingService, ServiceError

SOURCES = ("tv", "line_in")
FAVORITES_EVERY = 600  # seconds between refreshes of the favorites list
STATES = {"PLAYING": "playing", "PAUSED_PLAYBACK": "paused", "STOPPED": "stopped", "TRANSITIONING": "loading"}


def _soco():
    import soco
    from soco import config
    config.REQUEST_TIMEOUT = 5
    return soco


class Speaker:
    """One Sonos player and what was last read from it."""

    def __init__(self, dev):
        self.dev = dev
        self.uid = dev.uid
        self.name = dev.player_name
        self.ip = dev.ip_address
        self.info = {}
        self.favorites = []
        self.favorites_at = 0
        self.lock = threading.Lock()

    def read(self):
        dev = self.dev
        coord = dev.group.coordinator if dev.group else dev
        transport = coord.get_current_transport_info()
        track = coord.get_current_track_info()
        if dev.is_playing_tv:
            source = "tv"
        elif dev.is_playing_line_in:
            source = "line_in"
        elif coord.is_playing_radio:
            source = "radio"
        else:
            source = "music"
        info = {
            "id": self.uid, "name": dev.player_name,
            "state": STATES.get(transport.get("current_transport_state"), "unknown"),
            "title": track.get("title") or None, "artist": track.get("artist") or None,
            "album": track.get("album") or None,
            "position": track.get("position") or None, "duration": track.get("duration") or None,
            "volume": int(dev.volume), "mute": bool(dev.mute), "source": source,
            "grouped": bool(dev.group and len(dev.group.members) > 1),
        }
        for mode in ("night_mode", "dialog_mode"):  # soundbars only
            try:
                value = getattr(dev, mode)
                info[mode] = None if value is None else bool(value)
            except Exception:
                info[mode] = None
        self.name = dev.player_name
        self.info = info


class SonosService(PollingService):
    key = "sonos"
    label = "Sonos"
    interval = 5

    def __init__(self, store, soco_module=None, discover_timeout=5, clock=time.time, **kw):
        super().__init__(clock=clock, **kw)
        self.store = store
        self._soco = soco_module
        self.discover_timeout = discover_timeout
        self.speakers = {}  # uid -> Speaker
        self._lock = threading.Lock()

    @property
    def soco(self):
        if self._soco is None:
            self._soco = _soco()
        return self._soco

    # --- finding speakers ---

    def _remembered(self):
        return [s for s in (self.store.get("settings").get("sonos") or []) if isinstance(s, dict) and s.get("ip")]

    def _remember(self):
        saved = [{"uid": sp.uid, "name": sp.name, "ip": sp.ip} for sp in self.speakers.values()]
        if saved != self.store.get("settings").get("sonos"):
            self.store.update_settings({"sonos": saved})

    def discover(self):
        found = self.soco.discover(timeout=self.discover_timeout) or set()
        visible = {d.uid: d for d in found}
        with self._lock:
            for uid, dev in visible.items():
                if uid not in self.speakers or self.speakers[uid].ip != dev.ip_address:
                    self.speakers[uid] = Speaker(dev)
        self._remember()
        return len(visible)

    def _load_remembered(self):
        for saved in self._remembered():
            if saved.get("uid") in self.speakers:
                continue
            try:
                dev = self.soco.SoCo(saved["ip"])
                if dev.uid != saved.get("uid"):  # the address now belongs to another speaker
                    continue
                self.speakers[dev.uid] = Speaker(dev)
            except Exception:
                continue

    # --- the loop ---

    def poll(self):
        if not self.speakers:
            self._load_remembered()
        if not self.speakers:
            if not self.discover():
                raise ServiceError("no Sonos speaker found on the home network")
        failed = []
        for sp in list(self.speakers.values()):
            try:
                with sp.lock:
                    sp.read()
                    if self.clock() - sp.favorites_at > FAVORITES_EVERY:
                        sp.favorites = [f.title for f in sp.dev.music_library.get_sonos_favorites()]
                        sp.favorites_at = self.clock()
            except Exception:
                failed.append(sp)
        if failed:
            self.discover()  # an address may have changed
            for sp in failed:
                current = self.speakers.get(sp.uid)
                if current is not None and current is not sp:
                    try:
                        with current.lock:
                            current.read()
                        failed.remove(sp)
                    except Exception:
                        pass
        self._remember()
        if failed:
            for sp in failed:
                sp.info = dict(sp.info, state="not answering")
            if len(failed) == len(self.speakers):
                raise ServiceError("the Sonos speaker isn't answering"
                                   if len(failed) == 1 else "no Sonos speaker is answering")

    def state(self):
        return {"speakers": [dict(sp.info or {"id": sp.uid, "name": sp.name, "state": "unknown"},
                                  favorites=list(sp.favorites))
                             for sp in self.speakers.values()]}

    def health_text(self):
        if not self.speakers:
            return None
        states = [sp.info.get("state") for sp in self.speakers.values()]
        if len(states) == 1:
            return {"playing": "playing", "paused": "paused", "stopped": "idle"}.get(states[0], "answering")
        return f"{states.count('playing')} of {len(states)} playing"

    # --- actions ---

    def _speaker(self, target):
        if not self.speakers:
            raise ServiceError("no Sonos speaker found yet")
        if target in (None, ""):
            if len(self.speakers) == 1:
                return next(iter(self.speakers.values()))
            raise KeyError("say which speaker")
        for sp in self.speakers.values():
            if target == sp.uid or str(target).lower() == str(sp.name).lower():
                return sp
        raise KeyError(f"no Sonos speaker {target!r}")

    def act(self, target, action, args=None):
        args = args or {}
        sp = self._speaker(target)
        dev = sp.dev
        coord = dev.group.coordinator if dev.group else dev
        try:
            with sp.lock:
                if action == "play":
                    coord.play()
                elif action == "pause":
                    coord.pause()
                elif action == "toggle":
                    if sp.info.get("state") == "playing":
                        coord.pause()
                    else:
                        coord.play()
                elif action == "next":
                    coord.next()
                elif action == "previous":
                    coord.previous()
                elif action == "volume":
                    level = int(args.get("level"))
                    cap = int(self.store.get("settings").get("sonos_max_volume") or 100)
                    dev.volume = max(0, min(cap, level))
                elif action == "mute":
                    dev.mute = bool(args.get("on", True))
                elif action == "source":
                    name = args.get("name")
                    if name == "tv":
                        dev.switch_to_tv()
                    elif name == "line_in":
                        dev.switch_to_line_in()
                    else:
                        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
                elif action == "favorite":
                    self._play_favorite(sp, coord, str(args.get("title", "")))
                elif action in ("night_mode", "dialog_mode"):
                    if sp.info.get(action) is None:
                        raise ServiceError(f"this speaker has no {action.replace('_', ' ')}")
                    setattr(dev, action, bool(args.get("on", True)))
                else:
                    raise ValueError(f"unknown Sonos action {action!r}")
                sp.read()
        except (ValueError, KeyError, ServiceError):
            raise
        except Exception as e:
            raise ServiceError(f"the Sonos speaker didn't take that ({type(e).__name__})") from e
        return {"speaker": sp.uid}

    def _play_favorite(self, sp, coord, title):
        favorites = {f.title: f for f in sp.dev.music_library.get_sonos_favorites()}
        fav = favorites.get(title)
        if fav is None:
            raise KeyError(f"no Sonos favorite {title!r}")
        try:  # radio stations and streams
            coord.play_uri(fav.get_uri(), fav.resource_meta_data)
        except Exception:  # playlists and albums go through the queue
            coord.clear_queue()
            coord.add_to_queue(fav.reference)
            coord.play_from_queue(0)
