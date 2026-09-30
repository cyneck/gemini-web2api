"""Configuration parsing, clamping, warnings and persistence."""
import json
import os
import shutil
import tempfile
import unittest

from gemini_web2api import config as config_module
from gemini_web2api.config import (CONFIG, account_cookie_path, account_limit,
                                   get_int, get_list, is_loopback_host,
                                   load_config, migrate_legacy_account,
                                   resolve_path, save_config, startup_warnings)


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.original = dict(CONFIG)
        self.original_path = config_module._CONFIG_PATH
        self.original_dir = config_module._CONFIG_DIR
        CONFIG.clear()
        CONFIG.update(config_module.DEFAULT_CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original)
        config_module._CONFIG_PATH = self.original_path
        config_module._CONFIG_DIR = self.original_dir


class NumericConfigTests(ConfigTestCase):
    def test_returns_default_when_missing(self):
        CONFIG.pop("stream_stall_timeout_sec", None)
        self.assertEqual(get_int("stream_stall_timeout_sec"), 120)

    def test_clamps_to_documented_range(self):
        CONFIG["retry_attempts"] = 0                 # below the floor
        self.assertEqual(get_int("retry_attempts"), 1)
        CONFIG["retry_attempts"] = 10_000            # absurd
        self.assertEqual(get_int("retry_attempts"), 20)

    def test_body_caps_cannot_be_disabled_by_a_typo(self):
        CONFIG["max_request_body_bytes"] = 0
        self.assertGreaterEqual(get_int("max_request_body_bytes"), 1024)
        CONFIG["max_request_body_bytes"] = 10 ** 12
        self.assertLessEqual(get_int("max_request_body_bytes"), 1024 * 1024 * 1024)

    def test_non_numeric_values_fall_back_to_default(self):
        CONFIG["request_timeout_sec"] = "not a number"
        self.assertEqual(get_int("request_timeout_sec"), 180)
        CONFIG["request_timeout_sec"] = None
        self.assertEqual(get_int("request_timeout_sec"), 180)

    def test_numeric_strings_are_accepted(self):
        CONFIG["port"] = "9000"
        self.assertEqual(get_int("port"), 9000)

    def test_stream_stall_timeout_may_be_disabled(self):
        CONFIG["stream_stall_timeout_sec"] = 0
        self.assertEqual(get_int("stream_stall_timeout_sec"), 0)


class ListConfigTests(ConfigTestCase):
    def test_list_is_returned_as_is(self):
        CONFIG["api_keys"] = ["a", "b"]
        self.assertEqual(get_list("api_keys"), ["a", "b"])

    def test_comma_separated_string_is_split(self):
        CONFIG["api_keys"] = "a, b ,c"
        self.assertEqual(get_list("api_keys"), ["a", "b", "c"])

    def test_none_falls_back_to_the_default(self):
        CONFIG["cors_origins"] = None
        self.assertEqual(get_list("cors_origins"), [])

    def test_unknown_type_falls_back(self):
        CONFIG["cors_origins"] = 42
        self.assertEqual(get_list("cors_origins"), [])

    def test_account_limit_uses_the_configured_cap(self):
        CONFIG["max_accounts"] = 3
        self.assertEqual(account_limit(), 3)


class LoopbackTests(unittest.TestCase):
    def test_loopback_hosts(self):
        for host in ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST"]:
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_public_hosts(self):
        for host in ["0.0.0.0", "::", "192.168.1.10", "example.com", "", None]:
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))


class StartupWarningTests(ConfigTestCase):
    def test_public_listener_without_keys_warns_loudly(self):
        CONFIG["host"] = "0.0.0.0"
        CONFIG["api_keys"] = []
        warnings = " ".join(startup_warnings())
        self.assertIn("api_keys 为空", warnings)
        self.assertIn("任何能访问该端口", warnings)

    def test_loopback_listener_still_mentions_missing_keys(self):
        CONFIG["host"] = "127.0.0.1"
        CONFIG["api_keys"] = []
        warnings = startup_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("api_keys 为空", warnings[0])

    def test_weak_keys_are_flagged(self):
        CONFIG["host"] = "127.0.0.1"
        CONFIG["api_keys"] = ["sk-gemini"]
        self.assertTrue(any("过弱" in w for w in startup_warnings()))

    def test_strong_key_on_loopback_is_quiet(self):
        CONFIG["host"] = "127.0.0.1"
        CONFIG["api_keys"] = ["a-very-long-random-api-key-value"]
        self.assertEqual(startup_warnings(), [])

    def test_private_image_fetch_is_flagged(self):
        CONFIG["host"] = "127.0.0.1"
        CONFIG["api_keys"] = ["a-very-long-random-api-key-value"]
        CONFIG["image_fetch_allow_private_hosts"] = True
        self.assertTrue(any("私有网段" in w for w in startup_warnings()))


class ConfigFileTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix="gw2a-config-")
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))

    def path(self, name="config.json"):
        return os.path.join(self.directory, name)

    def test_missing_file_is_created_on_request(self):
        target = self.path()
        load_config(target, create=True)
        self.assertTrue(os.path.exists(target))
        with open(target, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["host"], "127.0.0.1")
        self.assertEqual(config_module.config_path(), os.path.abspath(target))

    def test_missing_file_without_create_is_not_written(self):
        target = self.path()
        load_config(target)
        self.assertFalse(os.path.exists(target))

    def test_values_are_loaded_from_disk(self):
        target = self.path()
        with open(target, "w", encoding="utf-8") as handle:
            json.dump({"port": 1234, "host": "0.0.0.0"}, handle)
        load_config(target)
        self.assertEqual(CONFIG["port"], 1234)
        self.assertEqual(CONFIG["host"], "0.0.0.0")

    def test_save_roundtrips_current_values(self):
        target = self.path()
        load_config(target, create=True)
        CONFIG["port"] = 9999
        save_config()
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["port"], 9999)

    def test_save_without_a_loaded_path_raises(self):
        config_module._CONFIG_PATH = None
        with self.assertRaises(RuntimeError):
            save_config()

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        target = self.path()
        load_config(target, create=True)
        save_config()
        leftovers = [name for name in os.listdir(self.directory) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_relative_paths_resolve_against_the_config_directory(self):
        target = self.path()
        load_config(target, create=True)
        self.assertEqual(resolve_path("cookie.txt"),
                         os.path.join(os.path.abspath(self.directory), "cookie.txt"))
        self.assertEqual(resolve_path(os.path.join(self.directory, "abs.txt")),
                         os.path.join(self.directory, "abs.txt"))
        self.assertIsNone(resolve_path(None))

    def test_missing_path_returns_none(self):
        self.assertIsNone(resolve_path(None))


class LegacyMigrationTests(ConfigTestCase):
    def test_legacy_fields_become_account_zero(self):
        CONFIG.pop("accounts", None)
        CONFIG["cookie_file"] = "/tmp/legacy-cookie.txt"
        CONFIG["auth_user"] = "1"
        CONFIG["xsrf_token"] = "token"
        self.assertTrue(migrate_legacy_account())
        self.assertEqual(len(CONFIG["accounts"]), 1)
        self.assertEqual(CONFIG["accounts"][0]["cookie_file"], "/tmp/legacy-cookie.txt")
        self.assertEqual(CONFIG["active_account"], 0)
        self.assertEqual(account_cookie_path(), "/tmp/legacy-cookie.txt")

    def test_migration_is_idempotent(self):
        CONFIG.pop("accounts", None)
        CONFIG["cookie_file"] = "/tmp/legacy-cookie.txt"
        migrate_legacy_account()
        snapshot = list(CONFIG["accounts"])
        self.assertFalse(migrate_legacy_account())
        self.assertEqual(CONFIG["accounts"], snapshot)

    def test_no_migration_without_legacy_values(self):
        CONFIG["accounts"] = []
        CONFIG["cookie_file"] = None
        CONFIG["auth_user"] = None
        CONFIG["xsrf_token"] = None
        self.assertFalse(migrate_legacy_account())
        self.assertEqual(CONFIG["accounts"], [])


if __name__ == "__main__":
    unittest.main()
