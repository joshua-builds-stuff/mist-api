#!/usr/bin/env python3
"""Safely preview, apply, or roll back one Mist WLAN SSID change.

The script is read-only unless ``--apply`` is present.  A write additionally
requires ``--confirm-target SITE_ID/WLAN_ID``.  PUT requests are never retried
automatically, the request body contains only ``ssid``, and rollback records do
not contain the full WLAN object or any authentication material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mist_client import DEFAULT_BASE_URL, MistAPIError, MistClient, api_path_segment

logger = logging.getLogger("mist-wlan-update-stub")
ROLLBACK_RECORD_VERSION = 1
MAX_ROLLBACK_RECORD_BYTES = 16 * 1024


class WlanUpdateError(RuntimeError):
    """A safe-to-display validation, concurrency, or verification failure."""


def validate_ssid(ssid: str) -> str:
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise ValueError("SSID must contain between 1 and 32 UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in ssid):
        raise ValueError("SSID must not contain control characters")
    return ssid


def wlan_path(site_id: str, wlan_id: str) -> str:
    return f"/sites/{api_path_segment(site_id)}/wlans/{api_path_segment(wlan_id)}"


def object_sha256(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def read_wlan(client: MistClient, path: str) -> dict[str, Any]:
    payload = client.request_json("GET", path)
    if not isinstance(payload, dict):
        raise WlanUpdateError("Mist returned a non-object WLAN response")
    ssid = payload.get("ssid")
    if not isinstance(ssid, str):
        raise WlanUpdateError("Mist WLAN response did not contain a string ssid")
    return payload


def expected_confirmation(site_id: str, wlan_id: str) -> str:
    return f"{site_id}/{wlan_id}"


def _put_outcome_is_indeterminate(exc: MistAPIError) -> bool:
    """True when the PUT may have been applied despite the client error."""

    text = str(exc)
    return "Timeout" in text or "ConnectionError" in text


def _publish_rollback_record(
    client: MistClient,
    rollback_file: Path,
    site_id: str,
    wlan_id: str,
    before_ssid: str,
    applied_ssid: str,
) -> None:
    record = create_rollback_record(
        client,
        site_id,
        wlan_id,
        before_ssid,
        applied_ssid,
    )
    try:
        atomic_write_private_json(rollback_file, record)
    except (OSError, ValueError) as exc:
        raise WlanUpdateError(
            "SSID was updated but the rollback record could not be saved; "
            "the previous SSID is shown in the preview above"
        ) from exc
    logger.info("Saved minimal rollback record to %s", rollback_file)


def require_target_confirmation(
    confirmation: str | None, site_id: str, wlan_id: str
) -> None:
    expected = expected_confirmation(site_id, wlan_id)
    if confirmation != expected:
        raise WlanUpdateError(
            "Write blocked: --confirm-target must exactly equal the displayed SITE_ID/WLAN_ID"
        )


def atomic_write_private_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically save a small rollback record with owner-only permissions."""

    parent = path.parent
    if not parent.is_dir():
        raise ValueError(f"Rollback directory does not exist: {parent}")
    fd, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def create_rollback_record(
    client: MistClient,
    site_id: str,
    wlan_id: str,
    before_ssid: str,
    applied_ssid: str,
) -> dict[str, Any]:
    return {
        "version": ROLLBACK_RECORD_VERSION,
        "base_url": client.base_url,
        "site_id": site_id,
        "wlan_id": wlan_id,
        "before_ssid": before_ssid,
        "applied_ssid": applied_ssid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def load_rollback_record(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_ROLLBACK_RECORD_BYTES:
            raise WlanUpdateError("Rollback record exceeds the 16 KiB safety limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WlanUpdateError(f"Rollback record not found: {path}") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise WlanUpdateError(
            "Rollback record could not be read as valid JSON"
        ) from None

    required = {
        "version",
        "base_url",
        "site_id",
        "wlan_id",
        "before_ssid",
        "applied_ssid",
        "created_at",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise WlanUpdateError("Rollback record has an unexpected schema")
    if payload["version"] != ROLLBACK_RECORD_VERSION:
        raise WlanUpdateError("Rollback record version is not supported")
    for key in ("base_url", "site_id", "wlan_id", "created_at"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise WlanUpdateError(f"Rollback record contains an invalid {key}")
    validate_ssid(payload["before_ssid"])
    validate_ssid(payload["applied_ssid"])
    return payload


def print_change_preview(
    site_id: str, wlan_id: str, before_ssid: str, after_ssid: str
) -> None:
    print(f"Target: {expected_confirmation(site_id, wlan_id)}")
    print(f"Current SSID:  {json.dumps(before_ssid, ensure_ascii=False)}")
    print(f"Proposed SSID: {json.dumps(after_ssid, ensure_ascii=False)}")
    print('PUT body: {"ssid": <proposed SSID>}')


def apply_ssid_change(
    client: MistClient,
    *,
    site_id: str,
    wlan_id: str,
    desired_ssid: str,
    apply: bool,
    confirmation: str | None,
    rollback_file: Path | None,
    expected_current_ssid: str | None = None,
) -> bool:
    """Preview or perform a race-checked, minimal-body WLAN update."""

    desired_ssid = validate_ssid(desired_ssid)
    path = wlan_path(site_id, wlan_id)
    initial = read_wlan(client, path)
    current_ssid = initial["ssid"]
    if expected_current_ssid is not None and current_ssid != expected_current_ssid:
        raise WlanUpdateError(
            "Rollback blocked: current SSID no longer matches the state recorded after apply"
        )

    print_change_preview(site_id, wlan_id, current_ssid, desired_ssid)
    if current_ssid == desired_ssid:
        print("No change is needed.")
        return False
    if not apply:
        print(
            "Dry run complete; no changes applied. Add --apply and exact target confirmation to write."
        )
        return False

    require_target_confirmation(confirmation, site_id, wlan_id)

    # Re-read immediately before the PUT.  A full-object hash catches changes to
    # fields other than the SSID without persisting or printing those fields.
    pre_write = read_wlan(client, path)
    if object_sha256(pre_write) != object_sha256(initial):
        raise WlanUpdateError(
            "Write blocked: WLAN changed after the initial read; start over"
        )

    if rollback_file is not None:
        if rollback_file.exists() and rollback_file.is_dir():
            raise ValueError(f"Rollback path is a directory: {rollback_file}")
        if not rollback_file.parent.is_dir():
            raise ValueError(
                f"Rollback directory does not exist: {rollback_file.parent}"
            )

    # MistClient retries GET/HEAD/OPTIONS only by default, so this PUT is one
    # deliberate attempt.  Never replace this body with the complete GET object.
    put_error: MistAPIError | None = None
    try:
        client.request_json("PUT", path, json_body={"ssid": desired_ssid})
    except MistAPIError as exc:
        if not _put_outcome_is_indeterminate(exc):
            raise
        put_error = exc

    try:
        verified = read_wlan(client, path)
    except (MistAPIError, WlanUpdateError) as verify_exc:
        if put_error is not None:
            raise put_error from None
        # PUT returned success; the follow-up GET timed out or failed.
        if rollback_file is not None:
            _publish_rollback_record(
                client,
                rollback_file,
                site_id,
                wlan_id,
                current_ssid,
                desired_ssid,
            )
        raise WlanUpdateError(
            "SSID was updated but verification could not be completed; "
            "the previous SSID is shown in the preview above"
        ) from verify_exc

    if verified["ssid"] != desired_ssid:
        if put_error is not None:
            raise put_error
        raise WlanUpdateError(
            "Verification failed: WLAN SSID does not match the requested value"
        )

    # Publish only after GET confirms the desired SSID so a successful HTTP
    # response that did not apply cannot replace a still-valid previous record.
    if rollback_file is not None:
        _publish_rollback_record(
            client,
            rollback_file,
            site_id,
            wlan_id,
            current_ssid,
            desired_ssid,
        )

    print("Update verified.")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply one Mist WLAN SSID update."
    )
    parser.add_argument(
        "--site-id", default=os.environ.get("SITE_ID"), help="Mist site ID"
    )
    parser.add_argument(
        "--wlan-id", default=os.environ.get("WLAN_ID"), help="Mist WLAN ID"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MIST_BASE_URL"),
        help=f"Mist API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--allow-custom-base-url",
        action="store_true",
        help="Allow an explicitly supplied non-Mist HTTPS API host",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--new-ssid", help="Proposed WLAN SSID")
    action.add_argument(
        "--rollback", type=Path, metavar="FILE", help="Use a saved rollback record"
    )
    parser.add_argument(
        "--rollback-file",
        type=Path,
        default=Path("wlan_rollback.json"),
        help="Where a successful apply can be rolled back from",
    )
    parser.add_argument("--apply", action="store_true", help="Actually send one PUT")
    parser.add_argument(
        "--confirm-target",
        help="Required with --apply; must exactly equal SITE_ID/WLAN_ID",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    token = os.environ.get("MIST_API_TOKEN")
    if not token:
        raise SystemExit("Set MIST_API_TOKEN in your environment.")

    rollback_record: dict[str, Any] | None = None
    if args.rollback is not None:
        rollback_record = load_rollback_record(args.rollback)
        site_id = args.site_id or rollback_record["site_id"]
        wlan_id = args.wlan_id or rollback_record["wlan_id"]
        if (
            site_id != rollback_record["site_id"]
            or wlan_id != rollback_record["wlan_id"]
        ):
            raise WlanUpdateError(
                "CLI target does not match the rollback record target"
            )
        base_url = args.base_url or rollback_record["base_url"]
        desired_ssid = rollback_record["before_ssid"]
        expected_current_ssid = rollback_record["applied_ssid"]
        output_rollback_file = None
    else:
        site_id = args.site_id
        wlan_id = args.wlan_id
        base_url = args.base_url or DEFAULT_BASE_URL
        desired_ssid = args.new_ssid
        expected_current_ssid = None
        output_rollback_file = args.rollback_file

    if not site_id:
        raise SystemExit("Set SITE_ID or pass --site-id.")
    if not wlan_id:
        raise SystemExit("Set WLAN_ID or pass --wlan-id.")
    if desired_ssid is None:
        raise WlanUpdateError("No desired SSID was supplied")

    with MistClient(
        token,
        base_url=base_url,
        allow_custom_base_url=args.allow_custom_base_url,
    ) as client:
        if (
            rollback_record is not None
            and client.base_url != rollback_record["base_url"]
        ):
            raise WlanUpdateError(
                "Selected base URL does not match the rollback record"
            )
        apply_ssid_change(
            client,
            site_id=site_id,
            wlan_id=wlan_id,
            desired_ssid=desired_ssid,
            apply=args.apply,
            confirmation=args.confirm_target,
            rollback_file=output_rollback_file,
            expected_current_ssid=expected_current_ssid,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (MistAPIError, OSError, ValueError, WlanUpdateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
