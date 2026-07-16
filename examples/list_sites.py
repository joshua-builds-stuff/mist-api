#!/usr/bin/env python3
"""List every Juniper Mist site for an organization (Python 3.10+).

Environment variables:
  MIST_API_TOKEN  Required. Mist API token.
  ORG_ID          Required unless --org-id is provided.
  MIST_BASE_URL   Optional. Defaults to https://api.mist.com/api/v1.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from mist_client import DEFAULT_BASE_URL, MistAPIError, MistClient, api_path_segment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List every Juniper Mist site for an org."
    )
    parser.add_argument(
        "--org-id", default=os.environ.get("ORG_ID"), help="Mist org ID"
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

    with MistClient(
        token,
        base_url=args.base_url,
        allow_custom_base_url=args.allow_custom_base_url,
    ) as client:
        sites = client.paginate(
            f"/orgs/{api_path_segment(args.org_id)}/sites",
            page_size=args.page_size,
        )
        for site in sites:
            print(
                json.dumps(
                    {
                        "name": site.get("name"),
                        "id": site.get("id"),
                        "timezone": site.get("timezone"),
                    },
                    sort_keys=True,
                )
            )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (MistAPIError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
