#!/usr/bin/env python3
"""Export Mist site device statistics to an injection-safe CSV (Python 3.10+).

The export defaults to APs, matching the endpoint's normal use.  Select a
different supported device type with ``--device-type``.  Output is written to a
temporary file and atomically moved into place only after the export succeeds.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from mist_client import DEFAULT_BASE_URL, MistAPIError, MistClient, api_path_segment

logger = logging.getLogger("mist-devices-export")
FIELDNAMES = ["name", "hostname", "serial", "model", "type", "status", "site_id"]
FORMULA_PREFIXES = frozenset({"=", "+", "-", "@"})


def find_site_id(client: MistClient, org_id: str, site_name: str) -> str:
    """Resolve one site name, rejecting ambiguous case-insensitive matches."""

    target = site_name.strip().casefold()
    sites_path = f"/orgs/{api_path_segment(org_id)}/sites"
    matches = [
        site
        for site in client.paginate(sites_path)
        if str(site.get("name") or "").strip().casefold() == target
    ]
    if not matches:
        raise ValueError(f"Site not found: {site_name}")
    if len(matches) > 1:
        raise ValueError(
            f"Site name is ambiguous ({len(matches)} case-insensitive matches): {site_name}"
        )
    site_id = matches[0].get("id")
    if not isinstance(site_id, str) or not site_id:
        raise MistAPIError("Matched site did not contain a valid id")
    return site_id


def safe_csv_cell(value: Any) -> str:
    """Neutralize values that spreadsheet programs may interpret as formulas."""

    if value is None:
        return ""
    text = str(value)
    candidate = text.lstrip(" \t\r\n")
    if (candidate and candidate[0] in FORMULA_PREFIXES) or text.startswith(
        ("\t", "\r", "\n")
    ):
        return f"'{text}"
    return text


def device_csv_row(device: Mapping[str, Any], site_id: str) -> dict[str, str]:
    values = {
        "name": device.get("name"),
        "hostname": device.get("hostname") or device.get("name"),
        "serial": device.get("serial"),
        "model": device.get("model"),
        "type": device.get("type"),
        "status": device.get("status"),
        "site_id": site_id,
    }
    return {key: safe_csv_cell(value) for key, value in values.items()}


def atomic_write_csv(
    output: str | os.PathLike[str],
    devices: Iterable[Mapping[str, Any]],
    site_id: str,
) -> int:
    """Write CSV completely, fsync it, then atomically replace the destination."""

    destination = Path(output)
    parent = destination.parent
    if not parent.is_dir():
        raise ValueError(f"Output directory does not exist: {parent}")

    fd, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    count = 0
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            for device in devices:
                writer.writerow(device_csv_row(device, site_id))
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Mist site device stats to CSV."
    )
    parser.add_argument(
        "--org-id", default=os.environ.get("ORG_ID"), help="Mist org ID"
    )
    parser.add_argument(
        "--site-name",
        default=os.environ.get("TARGET_SITE_NAME"),
        help="Target site name (must be unique ignoring case)",
    )
    parser.add_argument(
        "--device-type",
        choices=("ap", "switch", "gateway"),
        default="ap",
        help="Device type to export (default: ap)",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("OUTPUT_CSV", "mist_site_devices.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MIST_BASE_URL", DEFAULT_BASE_URL),
        help="Mist API base URL",
    )
    parser.add_argument(
        "--allow-custom-base-url",
        action="store_true",
        help="Allow an explicitly supplied non-Mist HTTPS API host",
    )
    parser.add_argument(
        "--page-size", type=int, default=100, help="Results per API page"
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()

    token = os.environ.get("MIST_API_TOKEN")
    if not token:
        raise SystemExit("Set MIST_API_TOKEN in your environment.")
    if not args.org_id:
        raise SystemExit("Set ORG_ID or pass --org-id.")
    if not args.site_name:
        raise SystemExit("Set TARGET_SITE_NAME or pass --site-name.")

    with MistClient(
        token,
        base_url=args.base_url,
        allow_custom_base_url=args.allow_custom_base_url,
    ) as client:
        site_id = find_site_id(client, args.org_id, args.site_name)
        devices = client.paginate(
            f"/sites/{api_path_segment(site_id)}/stats/devices",
            params={"type": args.device_type},
            page_size=args.page_size,
        )
        count = atomic_write_csv(args.output, devices, site_id)

    logger.info("Wrote %s device rows to %s", count, args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (MistAPIError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
