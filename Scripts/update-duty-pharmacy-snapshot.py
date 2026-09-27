#!/usr/bin/env python3
"""Publish one validated İzmir duty-pharmacy snapshot per service day."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SOURCE_URL = "https://openapi.izmir.bel.tr/api/ibb/nobetcieczaneler"
SOURCE_PAGE = "https://acikveri.bizizmir.com/dataset/nobetci-eczaneler-ve-eczane-listesi"
LICENSE_URL = "https://acikveri.bizizmir.com/tr/license"
ISTANBUL = ZoneInfo("Europe/Istanbul")
WHITESPACE = re.compile(r"\s+")


class SnapshotError(RuntimeError):
    pass


def compact(value: Any) -> str:
    return WHITESPACE.sub(" ", str(value or "")).strip()


def source_day(value: Any) -> str | None:
    text = compact(value)
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None


def service_day(now: datetime) -> str:
    local = now.astimezone(ISTANBUL)
    if local.time() < time(9, 0):
        local -= timedelta(days=1)
    return local.date().isoformat()


def parse_coordinate(value: Any, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def normalize_phone(value: Any) -> str | None:
    digits = "".join(character for character in compact(value) if character.isdigit())
    if digits.startswith("90") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits if len(digits) in (10, 11) else None


def stable_id(item: dict[str, Any], name: str, address: str, district: str, coordinate: dict[str, float] | None) -> str:
    provider_id = item.get("EczaneId")
    if isinstance(provider_id, int) and provider_id > 0:
        return f"izmir-open-data:{provider_id}"
    basis = "|".join(
        [name.casefold(), district.casefold(), address.casefold(), json.dumps(coordinate, sort_keys=True)]
    )
    return "izmir-open-data:generated:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def normalize(items: list[dict[str, Any]], expected_service_day: str, fetched_at: datetime) -> dict[str, Any]:
    dated_items = [item for item in items if source_day(item.get("Tarih")) == expected_service_day]
    if not dated_items:
        raise SnapshotError("Kaynak beklenen hizmet günü için kayıt döndürmedi.")

    pharmacies: list[dict[str, Any]] = []
    seen: set[str] = set()
    coordinate_count = 0
    for item in dated_items:
        name = compact(item.get("Adi"))
        address = compact(item.get("Adres"))
        district = compact(item.get("Bolge"))
        if not name or not address:
            continue

        # The municipal API exposes latitude in LokasyonX and longitude in LokasyonY.
        latitude = parse_coordinate(item.get("LokasyonX"), -90, 90)
        longitude = parse_coordinate(item.get("LokasyonY"), -180, 180)
        coordinate = None
        if latitude is not None and longitude is not None:
            coordinate = {"latitude": latitude, "longitude": longitude}
            coordinate_count += 1

        identifier = stable_id(item, name, address, district, coordinate)
        duplicate_key = f"{name.casefold()}|{address.casefold()}"
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)

        pharmacy: dict[str, Any] = {
            "id": identifier,
            "name": name,
            "city": "İzmir",
            "address": address,
        }
        if district:
            pharmacy["district"] = district
        if coordinate:
            pharmacy["coordinate"] = coordinate
        phone = normalize_phone(item.get("Telefon"))
        if phone:
            pharmacy["phone"] = phone
        duty_note = compact(item.get("BolgeAciklama"))
        if duty_note:
            pharmacy["dutyNote"] = duty_note
        pharmacies.append(pharmacy)

    if not pharmacies:
        raise SnapshotError("Kaynakta yayınlanabilir eczane kaydı bulunamadı.")
    if coordinate_count / len(pharmacies) < 0.8:
        raise SnapshotError("Koordinat kapsamı beklenmedik ölçüde düşük.")

    service_date = datetime.fromisoformat(expected_service_day).date()
    valid_from = datetime.combine(service_date, time(9, 0), ISTANBUL)
    valid_until = valid_from + timedelta(days=1)
    return {
        "schemaVersion": 1,
        "id": f"izmir-open-data:{expected_service_day}",
        "serviceDate": expected_service_day,
        "validFrom": valid_from.isoformat(timespec="seconds"),
        "validUntil": valid_until.isoformat(timespec="seconds"),
        "fetchedAt": fetched_at.astimezone(ISTANBUL).isoformat(timespec="seconds"),
        "source": {
            "id": "izmir-open-data",
            "name": "İzmir Büyükşehir Belediyesi Açık Veri",
            "url": SOURCE_PAGE,
            "licenseName": "İzmir Açık Veri Lisansı (CC BY 4.0)",
            "licenseURL": LICENSE_URL,
        },
        "coverage": {"country": "Türkiye", "city": "İzmir"},
        "validationStatus": "validated",
        "pharmacies": sorted(pharmacies, key=lambda value: (value.get("district", ""), value["name"])),
    }


def load_existing(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def fetch_items(input_path: Path | None) -> list[dict[str, Any]]:
    if input_path:
        payload = input_path.read_bytes()
    else:
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Pilly-Daily-Snapshot/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status < 200 or response.status >= 300:
                raise SnapshotError(f"Kaynak HTTP {response.status} döndürdü.")
            payload = response.read()
    value = json.loads(payload)
    if not isinstance(value, list):
        raise SnapshotError("Kaynak yanıtı bir kayıt listesi değil.")
    return [item for item in value if isinstance(item, dict)]


def publish(output: Path, archive_dir: Path, now: datetime, input_path: Path | None = None) -> bool:
    expected_day = service_day(now)
    existing = load_existing(output)
    if existing and existing.get("serviceDate") == expected_day and existing.get("validationStatus") == "validated":
        print(f"{expected_day} snapshot zaten doğrulanmış; kaynak çağrısı yapılmadı.")
        return False

    snapshot = normalize(fetch_items(input_path), expected_day, now)
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8")
    (archive_dir / f"{expected_day}.json").write_text(encoded, encoding="utf-8")
    print(f"{expected_day} için {len(snapshot['pharmacies'])} eczane yayınlandı.")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="Test/yerel kullanım için ham JSON dosyası")
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--archive-dir", type=Path, default=Path("data/archive"))
    parser.add_argument("--now", help="ISO-8601 test zamanı")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(ISTANBUL)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ISTANBUL)
    try:
        publish(args.output, args.archive_dir, now, args.input)
    except (OSError, ValueError, json.JSONDecodeError, SnapshotError) as error:
        print(f"Snapshot yayınlanamadı: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
