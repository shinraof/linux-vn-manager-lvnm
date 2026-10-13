import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lvnm"))

from PySide6.QtWidgets import QApplication
import config
from model.game_card import GameCard
from discord_presence import (
    DiscordPresenceManager, DiscordRpc, GameActivity, PresenceWorker, image_url, vndb_title,
)

APP = QApplication.instance() or QApplication([])
CLIENT_ID = "123456789012345678"
COVER = "https://t.vndb.org/cv/01/12301.jpg"
METADATA = {
    "title": "Romanized title", "alttitle": "原題",
    "titles": [
        {"lang": "zh", "title": "原題", "main": True},
        {"lang": "ja", "title": "日本語訳", "main": False},
        {"lang": "en", "title": "English title", "main": False},
    ],
    "image": {"url": COVER},
}


class MemorySettings(dict):
    def set(self, key, value):
        self[key] = value


def card(name="Library title"):
    return GameCard(name=name, path="/tmp/game.exe", prefix="Native", cover_source_url=COVER, vndb="v17", ogtitle="Cached native title")


class PresenceManagerTests(unittest.TestCase):
    def setUp(self):
        self.settings = MemorySettings({config.USER_CONF_DISCORD_CLIENT_ID: CLIENT_ID})
        self.worker = Mock()
        self.factory = Mock(return_value=self.worker)
        self.manager = DiscordPresenceManager(self.settings, self.factory)
        self.addCleanup(self.manager.shutdown)

    def test_disabled_by_default_and_enabling_during_a_game(self):
        self.manager.game_started(card())
        self.factory.assert_not_called()
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.manager.refresh()
        self.factory.assert_called_once()
        self.assertEqual(self.worker.publish.call_args.args[1].name, "Library title")

    def test_fresh_profile_uses_bundled_application_id(self):
        self.settings.pop(config.USER_CONF_DISCORD_CLIENT_ID)
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.manager.game_started(card())
        self.factory.assert_called_once()
        self.assertEqual(self.worker.publish.call_args.args[0], config.DISCORD_APPLICATION_ID)
        self.assertEqual(self.worker.publish.call_args.args[1].name, "Library title")
        self.assertNotIn(config.USER_CONF_DISCORD_CLIENT_ID, self.settings)

    def test_valid_legacy_application_id_is_preserved(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.settings[config.USER_CONF_DISCORD_CLIENT_ID] = f" {CLIENT_ID} "
        self.manager.game_started(card())
        self.assertEqual(self.worker.publish.call_args.args[0], CLIENT_ID)
        self.assertEqual(self.settings[config.USER_CONF_DISCORD_CLIENT_ID], f" {CLIENT_ID} ")

    def test_invalid_saved_application_id_falls_back_to_bundled_id(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        for value in (None, "", "token", "123", "１" * 18, "1" * 21):
            self.settings[config.USER_CONF_DISCORD_CLIENT_ID] = value
            self.manager.game_started(card())
            self.manager.refresh()
            self.assertEqual(self.worker.publish.call_args.args[0], config.DISCORD_APPLICATION_ID)

    def test_missing_build_id_does_not_connect_without_a_legacy_override(self):
        self.settings.pop(config.USER_CONF_DISCORD_CLIENT_ID)
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        with patch.object(config, "DISCORD_APPLICATION_ID", ""):
            self.manager.game_started(card())
        self.factory.assert_not_called()

    def test_latest_game_and_restore_previous_start_time(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        with patch("discord_presence.time.time", return_value=100):
            self.manager.game_started(card("First"))
        with patch("discord_presence.time.time", return_value=200):
            self.manager.game_started(card("Second"))
        self.assertEqual(self.worker.publish.call_args.args[1].name, "Second")
        self.manager.game_stopped("Second")
        restored = self.worker.publish.call_args.args[1]
        self.assertEqual((restored.name, restored.started), ("First", 100))
        self.manager.game_stopped("First")
        self.worker.publish.assert_called_with(CLIENT_ID, None)

    def test_disabling_and_changing_title_mode_preserves_elapsed_time(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.manager.game_started(card())
        started = self.worker.publish.call_args.args[1].started
        self.settings[config.USER_CONF_DISCORD_TITLE_MODE] = "native"
        self.manager.refresh()
        activity = self.worker.publish.call_args.args[1]
        self.assertEqual((activity.title_mode, activity.started), ("native", started))
        self.settings[config.USER_CONF_DISCORD_ENABLED] = False
        self.manager.refresh()
        self.worker.publish.assert_called_with(CLIENT_ID, None)

    def test_explicit_cover_preference_updates_running_game_without_resetting_time(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.manager.game_started(card())
        original = self.worker.publish.call_args.args[1]
        self.assertFalse(original.hide_explicit_covers)
        self.settings[config.USER_CONF_DISCORD_HIDE_EXPLICIT_COVERS] = True
        self.manager.refresh()
        filtered = self.worker.publish.call_args.args[1]
        self.assertTrue(filtered.hide_explicit_covers)
        self.assertEqual(filtered.started, original.started)
        self.factory.assert_called_once()
        self.settings[config.USER_CONF_DISCORD_HIDE_EXPLICIT_COVERS] = False
        self.manager.refresh()
        self.assertFalse(self.worker.publish.call_args.args[1].hide_explicit_covers)

    def test_worker_start_failure_does_not_propagate_to_game_launch(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.factory.side_effect = OSError("No threads available")
        with self.assertLogs("discord_presence", level="WARNING"):
            self.manager.game_started(card())
        self.assertIsNone(self.manager._worker)

    def test_shutdown_is_idempotent_and_ignores_later_refresh(self):
        self.settings[config.USER_CONF_DISCORD_ENABLED] = True
        self.manager.game_started(card())
        self.manager.shutdown()
        self.manager.shutdown()
        self.worker.shutdown.assert_called_once()
        self.worker.publish.reset_mock()
        self.manager.refresh()
        self.worker.publish.assert_not_called()


class MetadataTests(unittest.TestCase):
    def setUp(self):
        # Payload construction has no thread or Qt dependency.
        self.worker = object.__new__(PresenceWorker)
        self.worker._metadata_cache = {}
        self.activity = GameActivity("Library title", "", "v17", 100)

    def response(self, data=METADATA):
        response = Mock()
        response.json.return_value = {"results": [data]}
        return response

    def test_native_title_uses_original_language_instead_of_assuming_japanese(self):
        self.assertEqual(vndb_title(METADATA, "native", "Fallback"), "原題")

    def test_translated_title_and_missing_english_fallback(self):
        self.assertEqual(vndb_title(METADATA, "translated", "Fallback"), "English title")
        self.assertEqual(vndb_title({"title": "Romanized"}, "translated", "Fallback"), "Romanized")
        self.assertEqual(vndb_title({}, "translated", "Fallback"), "Fallback")

    def test_native_alttitle_and_cached_title_fallback(self):
        self.assertEqual(vndb_title({"alttitle": "原題"}, "native", "Cached"), "原題")
        self.assertEqual(vndb_title({}, "native", "Cached"), "Cached")

    @patch("discord_presence.requests.post")
    def test_vndb_title_and_cover_share_one_cached_request(self, post):
        post.return_value = self.response()
        native = GameActivity("Library", "", "v17", 100, "native")
        english = GameActivity("Library", "", "v17", 100, "translated")
        payload = self.worker._payload(native)
        self.assertEqual((payload["name"], payload["large_image"], payload["start"]), ("原題", COVER, 100))
        self.assertEqual(self.worker._payload(english)["details"], "English title")
        post.assert_called_once()
        request = post.call_args.kwargs
        self.assertEqual(request["json"]["filters"], ["id", "=", "v17"])
        self.assertIn("titles.main", request["json"]["fields"])
        self.assertIn("image.sexual", request["json"]["fields"])
        self.assertEqual(request["timeout"], 5)

    @patch("discord_presence.requests.post")
    def test_saved_online_cover_and_library_name_need_no_vndb_request(self, post):
        payload = self.worker._payload(GameActivity("My game", COVER, "v17", 100))
        self.assertEqual((payload["name"], payload["large_image"]), ("My game", COVER))
        post.assert_not_called()

    @patch("discord_presence.requests.post")
    def test_saved_cover_is_preserved_when_resolving_titles(self, post):
        post.return_value = self.response()
        steam_cover = "https://cdn2.steamgriddb.com/grid/custom.png"
        activity = GameActivity("Library", steam_cover, "v17", 100, "translated")
        self.assertEqual(self.worker._payload(activity)["large_image"], steam_cover)

    @patch("discord_presence.requests.post", side_effect=OSError("Offline"))
    def test_offline_vndb_falls_back_and_throttles_retries(self, post):
        activity = GameActivity("Library", "", "v17", 100, "native", "Cached native")
        first = self.worker._payload(activity)
        second = self.worker._payload(activity)
        self.assertEqual(first["name"], "Cached native")
        self.assertEqual(first, second)
        self.assertNotIn("large_image", first)
        post.assert_called_once()
        self.worker._metadata_cache["v17"] = ({}, 0)
        self.worker._payload(activity)
        self.assertEqual(post.call_count, 2)

    @patch("discord_presence.requests.post")
    def test_missing_id_and_local_files_show_name_without_an_image(self, post):
        payload = self.worker._payload(GameActivity("X", "/tmp/icon.png", "", 100, "translated"))
        self.assertEqual(payload["details"], "Playing X")
        self.assertNotIn("large_image", payload)
        post.assert_not_called()

    def test_image_urls_reject_local_files_and_credentials(self):
        for value in ("", "/tmp/icon.png", "file:///tmp/a.jpg", "http://example.com/a.png", "https://user:password@example.com/a.png", "https://["):
            self.assertEqual(image_url(value), "")
        self.assertEqual(image_url(COVER), COVER)

    @patch("discord_presence.requests.post")
    def test_explicit_vndb_covers_hidden_for_saved_and_fetched_urls(self, post):
        for rating in (1.5, 1.8, 2):
            for saved_cover in ("", COVER):
                with self.subTest(rating=rating, saved_cover=saved_cover):
                    self.worker._metadata_cache.clear()
                    post.return_value = self.response({**METADATA, "image": {"url": COVER, "sexual": rating}})
                    activity = GameActivity("Library", saved_cover, "v17", 100, "native", hide_explicit_covers=True)
                    payload = self.worker._payload(activity)
                    self.assertNotIn("large_image", payload)
                    self.assertNotIn("large_text", payload)
                    self.assertEqual((payload["name"], payload["start"]), ("原題", 100))

    @patch("discord_presence.requests.post")
    def test_safe_and_suggestive_covers_remain_visible_with_filter_enabled(self, post):
        for rating in (0, 0.3, 1, 1.49):
            with self.subTest(rating=rating):
                self.worker._metadata_cache.clear()
                post.return_value = self.response({**METADATA, "image": {"url": COVER, "sexual": rating}})
                activity = GameActivity("Library", COVER, "v17", 100, hide_explicit_covers=True)
                self.assertEqual(self.worker._payload(activity)["large_image"], COVER)
        self.assertEqual(post.call_count, 4)

    @patch("discord_presence.requests.post")
    def test_filter_off_preserves_explicit_cover(self, post):
        post.return_value = self.response({**METADATA, "image": {"url": COVER, "sexual": 2}})
        activity = GameActivity("Library", "", "v17", 100)
        self.assertEqual(self.worker._payload(activity)["large_image"], COVER)

    @patch("discord_presence.requests.post", side_effect=OSError("Offline"))
    def test_unavailable_rating_hides_saved_vndb_cover_but_keeps_title(self, post):
        activity = GameActivity("Library", COVER, "v17", 100, hide_explicit_covers=True)
        payload = self.worker._payload(activity)
        self.assertNotIn("large_image", payload)
        self.assertEqual(payload["details"], "Library")
        post.assert_called_once()

    @patch("discord_presence.requests.post")
    def test_missing_and_invalid_ratings_do_not_expose_unchecked_covers(self, post):
        for rating in (None, "explicit", True, float("nan"), 3):
            with self.subTest(rating=rating):
                self.worker._metadata_cache.clear()
                post.return_value = self.response({**METADATA, "image": {"url": COVER, "sexual": rating}})
                activity = GameActivity("Library", COVER, "v17", 100, hide_explicit_covers=True)
                self.assertNotIn("large_image", self.worker._payload(activity))

    @patch("discord_presence.requests.post")
    def test_filter_does_not_apply_vndb_rating_to_steamgriddb_cover(self, post):
        post.return_value = self.response({**METADATA, "image": {"url": COVER, "sexual": 2}})
        steam_cover = "https://cdn2.steamgriddb.com/grid/custom.png"
        activity = GameActivity("Library", steam_cover, "v17", 100, "translated", hide_explicit_covers=True)
        self.assertEqual(self.worker._payload(activity)["large_image"], steam_cover)
        post.reset_mock()
        activity = GameActivity("Library", steam_cover, "sgdb:123", 100, hide_explicit_covers=True)
        self.assertEqual(self.worker._payload(activity)["large_image"], steam_cover)
        post.assert_not_called()

    @patch("discord_presence.requests.post")
    def test_rating_of_main_vndb_cover_does_not_certify_a_different_image(self, post):
        post.return_value = self.response({**METADATA, "image": {"url": COVER, "sexual": 0}})
        alternate = "https://t.vndb.org/cv/02/12302.jpg"
        activity = GameActivity("Library", alternate, "v17", 100, hide_explicit_covers=True)
        self.assertNotIn("large_image", self.worker._payload(activity))


class FastWorker(PresenceWorker):
    UPDATE_INTERVAL = 0.03


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.fail_connect = False
        self.fail_update = False
        owner = self

        class FakeRpc:
            def __init__(self, client_id):
                self.client_id = client_id

            def connect(self):
                if owner.fail_connect:
                    owner.fail_connect = False
                    raise OSError("Discord not running")

            def update(self, **payload):
                if owner.fail_update:
                    owner.fail_update = False
                    raise OSError("Discord restarted")
                owner.events.put(("update", self.client_id, payload))

            def clear(self):
                owner.events.put(("clear", self.client_id))

            def close(self):
                owner.events.put(("close", self.client_id))

        self.worker = FastWorker(FakeRpc)
        self.addCleanup(self.worker.shutdown)
        self.activity = GameActivity("Game", COVER, "", 100)

    def event(self, kind):
        for _ in range(30):
            event = self.events.get(timeout=2)
            if event[0] == kind:
                return event
        self.fail(f"Missing {kind} event")

    def test_game_exit_clears_and_disconnects(self):
        self.worker.publish(CLIENT_ID, self.activity)
        self.assertEqual(self.event("update")[2]["name"], "Game")
        self.worker.publish(CLIENT_ID, None)
        self.assertEqual(self.event("clear")[1], CLIENT_ID)
        self.event("close")

    def test_discord_started_late_is_retried(self):
        self.fail_connect = True
        self.worker.publish(CLIENT_ID, self.activity)
        self.assertEqual(self.event("update")[2]["start"], 100)

    def test_discord_restart_reconnects(self):
        self.worker.publish(CLIENT_ID, self.activity)
        self.event("update")
        self.fail_update = True
        self.event("close")
        self.assertEqual(self.event("update")[2]["name"], "Game")

    def test_switching_application_clears_previous_client(self):
        self.worker.publish(CLIENT_ID, self.activity)
        self.event("update")
        new_id = "234567890123456789"
        self.worker.publish(new_id, self.activity)
        self.assertEqual(self.event("clear")[1], CLIENT_ID)
        self.assertEqual(self.event("update")[1], new_id)

    def test_shutdown_clears_activity_and_stops_worker(self):
        self.worker.publish(CLIENT_ID, self.activity)
        self.event("update")
        self.worker.shutdown()
        self.event("clear")
        self.assertFalse(self.worker._thread.is_alive())


@unittest.skipIf(sys.platform == "win32", "Unix IPC protocol test")
class RpcProtocolTests(unittest.TestCase):
    def test_real_pypresence_handshake_activity_and_clear(self):
        frames = queue.Queue()
        failures = queue.Queue()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "discord-ipc-0")
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            server.settimeout(3)

            def read_exact(connection, length):
                data = b""
                while len(data) < length:
                    chunk = connection.recv(length - len(data))
                    if not chunk:
                        raise EOFError
                    data += chunk
                return data

            def serve():
                try:
                    with server.accept()[0] as connection:
                        connection.settimeout(3)
                        for _ in range(4):
                            op, size = struct.unpack("<II", read_exact(connection, 8))
                            payload = json.loads(read_exact(connection, size))
                            frames.put((op, payload))
                            response = json.dumps({"evt": "READY" if op == 0 else None, "data": {}}).encode()
                            connection.sendall(struct.pack("<II", 1, len(response)) + response)
                except Exception as error:
                    failures.put(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                with patch("pypresence.baseclient.get_ipc_path", return_value=path):
                    rpc = DiscordRpc(CLIENT_ID)
                    try:
                        rpc.connect()
                        rpc.update(name="原題", details="原題", large_image=COVER, start=100)
                        rpc.update(name="原題", details="原題", start=100)
                        rpc.clear()
                    finally:
                        rpc.close()
                handshake = frames.get(timeout=3)
                activity = frames.get(timeout=3)[1]["args"]["activity"]
                hidden_activity = frames.get(timeout=3)[1]["args"]["activity"]
                clear_payload = frames.get(timeout=3)[1]
                self.assertEqual(handshake, (0, {"v": 1, "client_id": CLIENT_ID}))
                self.assertEqual(activity["name"], "原題")
                self.assertEqual(activity["status_display_type"], 2)
                self.assertEqual(activity["assets"]["large_image"], COVER)
                self.assertEqual(activity["timestamps"]["start"], 100)
                self.assertNotIn("assets", hidden_activity)
                self.assertEqual(hidden_activity["timestamps"]["start"], 100)
                self.assertEqual(clear_payload["cmd"], "SET_ACTIVITY")
                self.assertEqual(clear_payload["args"], {"pid": os.getpid()})
                self.assertTrue(rpc.loop.is_closed())
            finally:
                thread.join(timeout=3)
                server.close()
            if not failures.empty():
                raise failures.get()


class IntegrationTests(unittest.TestCase):
    def cli_controller(self, presence):
        import cli_controller
        with patch.object(cli_controller, "SettingsManager", return_value=MemorySettings()), \
             patch.object(cli_controller.DiscordPresenceManager, "get_instance", return_value=presence):
            controller = cli_controller.CliController()
        controller.update_game = Mock()
        return controller

    def test_cli_run_publishes_only_after_success_and_clears_on_exit(self):
        import cli_controller
        presence = Mock()
        controller = self.cli_controller(presence)
        runner = Mock()
        runner.run.return_value = True
        runner.is_running.side_effect = [False, False]
        runner.prefix_info = {"type": "native"}
        runner.game.exit_script = ""
        app = Mock()
        app.exec.side_effect = lambda: controller.monitor_timer.timeout.emit()
        with patch.object(cli_controller, "create_launcher", return_value=runner), \
             patch.object(cli_controller.GameManager, "get_game", return_value=card()), \
             patch.object(cli_controller.QCoreApplication, "instance", return_value=app), \
             patch.object(cli_controller.signal, "signal"):
            try:
                controller.headless_run("Library title")
                presence.game_started.assert_called_once()
                presence.game_stopped.assert_called_once_with("Library title")
                app.quit.assert_called_once()
            finally:
                if hasattr(controller, "monitor_timer"):
                    controller.monitor_timer.stop()

    def test_failed_cli_launch_does_not_publish_presence(self):
        import cli_controller
        presence = Mock()
        controller = self.cli_controller(presence)
        runner = Mock()
        runner.run.return_value = False
        runner.is_running.return_value = False
        runner.prefix_info = {"type": "native"}
        with patch.object(cli_controller, "create_launcher", return_value=runner), \
             patch.object(cli_controller.GameManager, "get_game", return_value=card()), \
             patch.object(cli_controller.signal, "signal"):
            with self.assertLogs("cli_controller", level="ERROR"):
                controller.headless_run("Library title")
        presence.game_started.assert_not_called()

    def test_external_tracking_publishes_only_when_process_is_detected(self):
        import cli_controller
        presence = Mock()
        controller = self.cli_controller(presence)
        game = card()
        game.path = "/tmp/game.exe"
        runner = Mock()
        runner.is_running.side_effect = [False, True, False]
        app = Mock()

        def monitor():
            controller.monitor_timer.timeout.emit()
            presence.game_started.assert_not_called()
            controller.monitor_timer.timeout.emit()
            presence.game_started.assert_called_once_with(game)
            controller.monitor_timer.timeout.emit()

        app.exec.side_effect = monitor
        with patch.object(cli_controller, "create_launcher", return_value=runner), \
             patch.object(cli_controller.GameManager, "get_game", return_value=game), \
             patch.object(cli_controller.QCoreApplication, "instance", return_value=app), \
             patch.object(cli_controller, "TrackingController"), \
             patch.object(cli_controller.signal, "signal"):
            try:
                controller.headless_timetracker("Library title")
                presence.game_stopped.assert_called_once_with("Library title")
                app.quit.assert_called_once()
            finally:
                if hasattr(controller, "monitor_timer"):
                    controller.monitor_timer.stop()

    def test_cli_shutdown_runs_even_when_the_launch_raises(self):
        presence = Mock()
        controller = self.cli_controller(presence)
        controller.headless_run = Mock(side_effect=RuntimeError("Launch failed"))
        with self.assertRaises(RuntimeError):
            controller.handle_args(SimpleNamespace(run="Game", steam=False))
        presence.shutdown.assert_called_once()

    def test_gui_launch_and_exit_publish_presence(self):
        import game_process_manager
        presence = Mock()
        runner = Mock()
        runner.run.return_value = True
        runner.is_running.return_value = False
        with patch.object(game_process_manager.DiscordPresenceManager, "get_instance", return_value=presence), \
             patch.object(game_process_manager, "create_launcher", return_value=runner), \
             patch.object(game_process_manager.GameManager, "get_game", return_value=card()):
            manager = game_process_manager.GameProcessManager()
            self.addCleanup(manager.monitor_timer.stop)
            self.assertTrue(manager.start_game("Library title", {}))
            presence.game_started.assert_called_once()
            manager._exit_hooks = [manager._clear_discord_presence]
            manager._check_active_runners()
            presence.game_stopped.assert_called_once_with("Library title")

    def test_failed_gui_launch_does_not_publish_presence(self):
        import game_process_manager
        presence = Mock()
        runner = Mock()
        runner.run.return_value = False
        with patch.object(game_process_manager.DiscordPresenceManager, "get_instance", return_value=presence), \
             patch.object(game_process_manager, "create_launcher", return_value=runner), \
             patch.object(game_process_manager.GameManager, "get_game", return_value=card()):
            manager = game_process_manager.GameProcessManager()
            self.addCleanup(manager.monitor_timer.stop)
            with self.assertRaises(RuntimeError):
                manager.start_game("Library title", {})
            presence.game_started.assert_not_called()

    def test_settings_controls_save_title_preference_and_disable_live_presence(self):
        from PySide6.QtCore import QSettings
        from ui.settings_tab import SettingsTab
        from ui.theme_manager import ThemeManager
        settings = MemorySettings({config.USER_CONF_DISCORD_CLIENT_ID: CLIENT_ID})
        presence = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch("ui.settings_tab.SettingsManager", return_value=settings), \
             patch.object(SettingsTab, "_start_background_checks"), \
             patch("ui.settings_tab.DiscordPresenceManager.get_instance", return_value=presence):
            theme = ThemeManager(QSettings(str(Path(directory) / "theme.ini"), QSettings.IniFormat))
            tab = SettingsTab(theme)
            try:
                self.assertFalse(tab.discord_enabled.isChecked())
                self.assertFalse(hasattr(tab, "discord_client_id"))
                self.assertFalse(hasattr(tab, "discord_id_hint"))
                self.assertFalse(tab.discord_hide_explicit_covers.isChecked())
                tab.discord_enabled.setChecked(True)
                tab.discord_title_mode.setCurrentIndex(tab.discord_title_mode.findData("native"))
                self.assertEqual(settings[config.USER_CONF_DISCORD_TITLE_MODE], "native")
                self.assertEqual(settings[config.USER_CONF_DISCORD_CLIENT_ID], CLIENT_ID)
                self.assertTrue(settings[config.USER_CONF_DISCORD_ENABLED])
                self.assertTrue(tab.discord_hide_explicit_covers.isEnabled())
                tab.discord_hide_explicit_covers.setChecked(True)
                self.assertTrue(settings[config.USER_CONF_DISCORD_HIDE_EXPLICIT_COVERS])
                tab.discord_enabled.setChecked(False)
                self.assertFalse(settings[config.USER_CONF_DISCORD_ENABLED])
                self.assertFalse(tab.discord_title_mode.isEnabled())
                self.assertFalse(tab.discord_hide_explicit_covers.isEnabled())
                self.assertGreaterEqual(presence.refresh.call_count, 4)
                reloaded = SettingsTab(theme)
                try:
                    self.assertTrue(reloaded.discord_hide_explicit_covers.isChecked())
                    self.assertFalse(hasattr(reloaded, "discord_client_id"))
                finally:
                    reloaded.deleteLater()
            finally:
                tab.deleteLater()


if __name__ == "__main__":
    unittest.main()
