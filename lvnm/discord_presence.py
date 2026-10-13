"""Optional Discord IPC integration; all network/IPC work stays off the Qt thread."""
import asyncio
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QCoreApplication, QObject, QTimer

import config
from settings_manager import SettingsManager

logger = logging.getLogger(__name__)


def image_url(value):
    """Discord fetches artwork itself and cannot read local cover files."""
    try:
        url = urlsplit(value or "")
        if url.scheme == "https" and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return ""


def is_vndb_image(url):
    host = urlsplit(url).hostname or ""
    return host == "vndb.org" or host.endswith(".vndb.org")


def hide_vndb_cover(cover, metadata):
    """Check the selected image, rather than applying a VN's rating to other art."""
    image = metadata.get("image") or {}
    if cover != image_url(image.get("url")):
        # Other providers have no VNDB rating. A different VNDB cover cannot be
        # verified using the current main image's rating, so withhold it.
        return is_vndb_image(cover)
    rating = image.get("sexual")
    if isinstance(rating, bool) or not isinstance(rating, (int, float)):
        return True
    if not math.isfinite(rating) or not 0 <= rating <= 2:
        return True
    # VNDB's averaged 0=safe, 1=suggestive, 2=explicit scale rounds to explicit
    # from 1.5 onward. Only the explicit category is filtered.
    return rating >= 1.5


@dataclass(frozen=True)
class GameActivity:
    name: str
    cover_url: str
    vndb: str
    started: int
    title_mode: str = "library"
    native_title: str = ""
    hide_explicit_covers: bool = False


def vndb_title(metadata, mode, fallback):
    """Use VNDB's main original title, or its English title when available."""
    titles = metadata.get("titles") or []
    if mode == "native":
        original = next((t.get("title") for t in titles if t.get("main") and t.get("title")), "")
        return original or metadata.get("alttitle") or fallback
    if mode == "translated":
        english = next((t.get("title") for t in titles if t.get("lang") == "en" and t.get("title")), "")
        return english or metadata.get("title") or fallback
    return fallback


class DiscordRpc:
    """Own the pypresence event loop and bound even the handshake timeout."""

    def __init__(self, client_id):
        from pypresence import Presence
        self.loop = asyncio.new_event_loop()
        self.client = Presence(client_id, loop=self.loop, connection_timeout=3, response_timeout=3)

    def connect(self):
        self.loop.run_until_complete(asyncio.wait_for(self.client.handshake(), timeout=5))

    def update(self, **payload):
        from pypresence import StatusDisplayType
        self.client.update(status_display_type=StatusDisplayType.DETAILS, **payload)

    def clear(self):
        self.client.clear()

    def close(self):
        # Presence.close() leaves the Unix stream open; explicitly dispose it,
        # including after a failed handshake, before disposing our event loop.
        try:
            writer = self.client.sock_writer
            if writer:
                writer.close()
                self.loop.run_until_complete(asyncio.wait_for(writer.wait_closed(), timeout=1))
        except Exception:
            logger.debug("Discord IPC stream closed with an error", exc_info=True)
        finally:
            self.loop.close()


class PresenceWorker:
    """Coalesce updates, retry a late/restarted Discord client, and clear promptly."""

    UPDATE_INTERVAL = 15

    def __init__(self, rpc_factory=DiscordRpc):
        self._rpc_factory = rpc_factory
        self._condition = threading.Condition()
        self._desired = None
        self._closed = False
        self._metadata_cache = {}
        self._thread = threading.Thread(target=self._run, name="discord-presence", daemon=True)
        self._thread.start()

    def publish(self, client_id, activity):
        with self._condition:
            self._desired = (client_id, activity) if client_id and activity else None
            self._condition.notify_all()

    def shutdown(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        # IPC can be unavailable. Never hold application exit hostage to it.
        self._thread.join(timeout=1)

    def _metadata(self, activity):
        if not re.fullmatch(r"v[1-9][0-9]*", activity.vndb or ""):
            return {}
        saved_cover = image_url(activity.cover_url)
        needs_rating = activity.hide_explicit_covers and is_vndb_image(saved_cover)
        if activity.title_mode == "library" and saved_cover and not needs_rating:
            return {}
        cached, retry_at = self._metadata_cache.get(activity.vndb, ({}, 0))
        if time.monotonic() >= retry_at:
            cached = {}
            retry_at = time.monotonic() + 60
            try:
                response = requests.post(
                    f"{config.VNDB_API_URL}/vn",
                    json={"filters": ["id", "=", activity.vndb],
                          "fields": "title, alttitle, titles.lang, titles.title, titles.main, image.url, image.sexual"},
                    timeout=5,
                )
                response.raise_for_status()
                results = response.json().get("results", [])
                if results:
                    cached = results[0]
                retry_at = float("inf")
            except Exception:
                logger.debug("Could not resolve Discord metadata from VNDB", exc_info=True)
            self._metadata_cache[activity.vndb] = (cached, retry_at)
        return cached

    def _payload(self, activity):
        metadata = self._metadata(activity)
        fallback = (activity.native_title or activity.name) if activity.title_mode == "native" else activity.name
        title = vndb_title(metadata, activity.title_mode, fallback).strip() or activity.name
        payload = {
            "name": title[:128],
            "details": title[:128] if len(title) > 1 else f"Playing {title}",
            "state": "via LVNM",
            "start": activity.started,
        }
        cover = image_url(activity.cover_url) or image_url((metadata.get("image") or {}).get("url"))
        if cover and activity.hide_explicit_covers and hide_vndb_cover(cover, metadata):
            cover = ""
        if cover:
            payload.update(large_image=cover, large_text=title[:128])
        return payload

    @staticmethod
    def _disconnect(rpc, clear=False):
        if rpc:
            try:
                if clear:
                    rpc.clear()
            except Exception:
                logger.debug("Could not clear Discord activity", exc_info=True)
            finally:
                try:
                    rpc.close()
                except Exception:
                    logger.debug("Could not close Discord IPC", exc_info=True)

    def _run(self):
        rpc = None
        connected_id = None
        next_update = 0
        failure_reported = False
        try:
            while True:
                with self._condition:
                    desired = self._desired
                    if self._closed:
                        break
                if desired is None or desired[0] != connected_id:
                    self._disconnect(rpc, clear=True)
                    rpc = None
                    connected_id = desired[0] if desired else None
                    next_update = 0
                if desired and time.monotonic() >= next_update:
                    try:
                        payload = self._payload(desired[1])
                        if rpc is None:
                            rpc = self._rpc_factory(desired[0])
                            rpc.connect()
                        with self._condition:
                            if self._closed or self._desired != desired:
                                continue
                        rpc.update(**payload)
                        failure_reported = False
                    except Exception as error:
                        if not failure_reported:
                            logger.info("Discord presence unavailable; will retry: %s", error)
                            failure_reported = True
                        self._disconnect(rpc)
                        rpc = None
                    next_update = time.monotonic() + self.UPDATE_INTERVAL
                with self._condition:
                    if not self._closed and self._desired == desired:
                        delay = max(0, next_update - time.monotonic()) if desired else None
                        self._condition.wait(timeout=delay)
        finally:
            self._disconnect(rpc, clear=True)


class DiscordPresenceManager(QObject):
    """Publish the most recently started game; restore the previous one on exit."""

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, settings=None, worker_factory=PresenceWorker):
        super().__init__()
        self.settings = settings if settings is not None else SettingsManager()
        self._worker_factory = worker_factory
        self._worker = None
        self._games = {}
        self._closed = False
        app = QCoreApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(15000)

    def game_started(self, game):
        if game and game.name not in self._games:
            self._games[game.name] = GameActivity(
                game.name, game.cover_source_url, game.vndb, int(time.time()),
                native_title=game.ogtitle,
            )
            self.refresh()

    def game_stopped(self, name):
        self._games.pop(name, None)
        self.refresh()

    def refresh(self):
        if self._closed:
            return
        client_id = str(self.settings.get(config.USER_CONF_DISCORD_CLIENT_ID, "")).strip()
        enabled = self.settings.get(config.USER_CONF_DISCORD_ENABLED, False)
        activity = next(reversed(self._games.values()), None)
        title_mode = self.settings.get(config.USER_CONF_DISCORD_TITLE_MODE, "library")
        if activity:
            activity = replace(
                activity,
                title_mode=title_mode if title_mode in {"library", "native", "translated"} else "library",
                hide_explicit_covers=bool(self.settings.get(config.USER_CONF_DISCORD_HIDE_EXPLICIT_COVERS, False)),
            )
        if not enabled or not re.fullmatch(r"[0-9]{17,20}", client_id):
            activity = None
        if activity and self._worker is None:
            try:
                self._worker = self._worker_factory()
            except Exception:
                logger.warning("Could not start Discord presence worker", exc_info=True)
                return
        if self._worker:
            self._worker.publish(client_id, activity)

    def shutdown(self):
        self._closed = True
        self._timer.stop()
        if self._worker:
            self._worker.shutdown()
            self._worker = None
