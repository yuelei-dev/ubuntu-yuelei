import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "enable_test_seedance.py"
SPEC = importlib.util.spec_from_file_location("enable_test_seedance", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _create_database(path):
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE feature_flags(
                   feature TEXT PRIMARY KEY,
                   enabled INTEGER NOT NULL DEFAULT 1,
                   updated_by TEXT,
                   updated_at INTEGER NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO feature_flags VALUES(?,?,?,?)",
            ("breakdown", 0, "existing", 10),
        )
        connection.commit()


class EnableTestSeedanceTests(unittest.TestCase):
    def test_enables_only_seedance_and_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            db_path = root / "feature_flags.db"
            backup_path = root / "backups" / "before.db"
            _create_database(db_path)

            result = MODULE.enable_seedance(
                db_path,
                backup_path,
                hostname=MODULE.TEST_HOSTNAME,
                confirmation=MODULE.TEST_SERVER,
                now=123,
            )

            self.assertTrue(result["enabled"])
            self.assertTrue(result["changed"])
            self.assertFalse(result["service_restart_required"])
            self.assertTrue(backup_path.is_file())
            with sqlite3.connect(str(db_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT enabled, updated_by, updated_at "
                        "FROM feature_flags WHERE feature=?",
                        (MODULE.FEATURE,),
                    ).fetchone(),
                    (1, MODULE.ACTOR, 123),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT enabled FROM feature_flags WHERE feature='breakdown'"
                    ).fetchone(),
                    (0,),
                )
            with sqlite3.connect(str(backup_path)) as backup:
                self.assertIsNone(
                    backup.execute(
                        "SELECT enabled FROM feature_flags WHERE feature=?",
                        (MODULE.FEATURE,),
                    ).fetchone()
                )

    def test_reports_idempotent_enable_without_disabling_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            db_path = root / "feature_flags.db"
            backup_path = root / "before.db"
            _create_database(db_path)
            with sqlite3.connect(str(db_path)) as connection:
                connection.execute(
                    "INSERT INTO feature_flags VALUES(?,?,?,?)",
                    (MODULE.FEATURE, 1, "previous", 11),
                )
                connection.commit()

            result = MODULE.enable_seedance(
                db_path,
                backup_path,
                hostname=MODULE.TEST_HOSTNAME,
                confirmation=MODULE.TEST_SERVER,
                now=124,
            )

            self.assertFalse(result["changed"])
            self.assertTrue(backup_path.is_file())

    def test_refuses_wrong_host_before_opening_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "missing.db"
            with self.assertRaisesRegex(RuntimeError, "outside the designated test"):
                MODULE.enable_seedance(
                    missing,
                    pathlib.Path(tmp) / "backup.db",
                    hostname="production-host",
                    confirmation=MODULE.TEST_SERVER,
                    now=1,
                )

    def test_refuses_wrong_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "missing.db"
            with self.assertRaisesRegex(RuntimeError, "exact test-server confirmation"):
                MODULE.enable_seedance(
                    missing,
                    pathlib.Path(tmp) / "backup.db",
                    hostname=MODULE.TEST_HOSTNAME,
                    confirmation="129.204.166.13",
                    now=1,
                )

    def test_refuses_missing_or_incompatible_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaises(FileNotFoundError):
                MODULE.enable_seedance(
                    root / "missing.db",
                    root / "missing-backup.db",
                    hostname=MODULE.TEST_HOSTNAME,
                    confirmation=MODULE.TEST_SERVER,
                    now=1,
                )

            bad_db = root / "bad.db"
            with sqlite3.connect(str(bad_db)) as connection:
                connection.execute("CREATE TABLE feature_flags(feature TEXT)")
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "schema is incompatible"):
                MODULE.enable_seedance(
                    bad_db,
                    root / "bad-backup.db",
                    hostname=MODULE.TEST_HOSTNAME,
                    confirmation=MODULE.TEST_SERVER,
                    now=1,
                )

    def test_never_overwrites_existing_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            db_path = root / "feature_flags.db"
            backup_path = root / "before.db"
            _create_database(db_path)
            backup_path.write_bytes(b"keep")

            with self.assertRaises(FileExistsError):
                MODULE.enable_seedance(
                    db_path,
                    backup_path,
                    hostname=MODULE.TEST_HOSTNAME,
                    confirmation=MODULE.TEST_SERVER,
                    now=1,
                )
            self.assertEqual(backup_path.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
