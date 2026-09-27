import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).parents[1] / "update-duty-pharmacy-snapshot.py"
SPEC = importlib.util.spec_from_file_location("update_duty_pharmacy_snapshot", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ISTANBUL = ZoneInfo("Europe/Istanbul")


def item(**changes):
    value = {
        "Tarih": "2026-09-27T08:00:00",
        "LokasyonX": "38.4210",
        "LokasyonY": "27.1420",
        "Adi": "Yakın Eczane",
        "Telefon": "+90 (232) 000 00 01",
        "Adres": "  Konak   Mahallesi  ",
        "Bolge": "Konak",
        "BolgeAciklama": "24:00'DEN SONRA İCAP NÖBETİ",
        "EczaneId": -1,
    }
    value.update(changes)
    return value


class DutyPharmacySnapshotUpdaterTests(unittest.TestCase):
    def test_normalizes_and_deduplicates_records(self):
        snapshot = MODULE.normalize(
            [item(), item(Telefon="02320000001")],
            "2026-09-27",
            dt.datetime(2026, 9, 27, 9, 15, tzinfo=ISTANBUL),
        )

        self.assertEqual(snapshot["serviceDate"], "2026-09-27")
        self.assertEqual(snapshot["validFrom"], "2026-09-27T09:00:00+03:00")
        self.assertEqual(snapshot["validUntil"], "2026-09-28T09:00:00+03:00")
        self.assertEqual(len(snapshot["pharmacies"]), 1)
        pharmacy = snapshot["pharmacies"][0]
        self.assertEqual(pharmacy["address"], "Konak Mahallesi")
        self.assertEqual(pharmacy["phone"], "02320000001")
        self.assertEqual(pharmacy["coordinate"], {"latitude": 38.421, "longitude": 27.142})
        self.assertTrue(pharmacy["id"].startswith("izmir-open-data:generated:"))

    def test_rejects_empty_wrong_day_and_mostly_coordinate_less_payloads(self):
        now = dt.datetime(2026, 9, 27, 9, 15, tzinfo=ISTANBUL)
        with self.assertRaises(MODULE.SnapshotError):
            MODULE.normalize([], "2026-09-27", now)
        with self.assertRaises(MODULE.SnapshotError):
            MODULE.normalize([item(Tarih="2026-09-26T08:00:00")], "2026-09-27", now)
        with self.assertRaises(MODULE.SnapshotError):
            MODULE.normalize([item(LokasyonX="bozuk")], "2026-09-27", now)

    def test_valid_snapshot_is_idempotent_and_does_not_read_source_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            output = root / "latest.json"
            archive = root / "archive"
            source.write_text(json.dumps([item()]), encoding="utf-8")
            now = dt.datetime(2026, 9, 27, 9, 15, tzinfo=ISTANBUL)

            self.assertTrue(MODULE.publish(output, archive, now, source))
            source.unlink()
            self.assertFalse(MODULE.publish(output, archive, now, source))
            self.assertTrue((archive / "2026-09-27.json").exists())

    def test_before_handover_uses_previous_service_day(self):
        now = dt.datetime(2026, 9, 28, 2, 0, tzinfo=ISTANBUL)
        self.assertEqual(MODULE.service_day(now), "2026-09-27")

    def test_positive_provider_id_is_stable(self):
        snapshot = MODULE.normalize(
            [item(EczaneId=42)],
            "2026-09-27",
            dt.datetime(2026, 9, 27, 9, 15, tzinfo=ISTANBUL),
        )
        self.assertEqual(snapshot["pharmacies"][0]["id"], "izmir-open-data:42")


if __name__ == "__main__":
    unittest.main()
