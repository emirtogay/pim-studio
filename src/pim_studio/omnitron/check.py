"""Connection check — `python -m pim_studio.omnitron.check`.

Confirms the credentials in `.env.local` work and reports what the token can
see, without ever printing the password or the token. Run it after filling in
`OMNITRON_PASSWORD`; it is the fastest way to find out whether the setup is
right before anything depends on it.

Read-only, like everything in this package.
"""

from __future__ import annotations

import sys

from .. import integrations
from .client import OmnitronError

#: The seven filter attributes we are starting with, keyed by the column name
#: seen in the 50-column export — `attributes__integration_yaka` implies the
#: attribute key `integration_yaka`. That inference is exactly what this check
#: confirms or refutes.
FILTER_ATTRIBUTE_KEYS = (
    "integration_yaka",       # collar
    "integration_kalip",      # fit
    "integration_kol_boyu",   # sleeve
    "integration_desen",      # pattern
    "integration_kumas_tipi", # fabric
    "integration_boy",        # length
    "integration_bel",        # waist
)


def main() -> int:
    try:
        client = integrations.pim_client()
    except OmnitronError as e:
        print(f"config: {e}", file=sys.stderr)
        return 1

    with client as api:
        print(f"host      {api.base_url}")
        try:
            # Cheapest authenticated call that proves the token works.
            body = api.get("/api/v1/attributes/", params={"limit": 1})
        except OmnitronError as e:
            print(f"\nauth failed: {e}", file=sys.stderr)
            print(
                "\nIf this is HTTP 400 or 401, the username or password in "
                ".env.local is wrong.\nIf it is 403, the account authenticated "
                "but lacks permission to read attributes.",
                file=sys.stderr,
            )
            return 1

        print("auth      ok (token obtained, not shown)")
        print(f"attributes visible: {body.get('count', '?')}")

        print("\n── the seven filter attributes ──")
        print(f"{'key':26} {'found':6} {'pk':>6}  {'data_type':12} {'localizable':11} erp_code")
        for key in FILTER_ATTRIBUTE_KEYS:
            try:
                r = api.get("/api/v1/attributes/", params={"key": key, "limit": 1})
            except OmnitronError as e:
                print(f"{key:26} ERROR  {e}")
                continue
            hits = r.get("results", [])
            if not hits:
                print(f"{key:26} {'no':6} {'—':>6}  {'—':12} {'—':11} —")
                continue
            a = hits[0]
            dt = a.get("data_type")
            dt = dt.get("value") if isinstance(dt, dict) else dt
            print(
                f"{key:26} {'yes':6} {a.get('pk') or a.get('id'):>6}  "
                f"{str(dt):12} {str(a.get('is_localizable')):11} {a.get('erp_code')}"
            )

        print("\n── channels (which markets exist) ──")
        try:
            # The channel object keys its id as `pk`, not `id` — the OpenAPI
            # model says `id` and the live instance disagrees. Read both, and
            # never format a value that might be None.
            for ch in api.iter_records("/api/v1/channels/", page_size=50):
                ct = ch.get("channel_type")
                ct = ct.get("value") if isinstance(ct, dict) else ct
                pk = ch.get("pk", ch.get("id"))
                live = "" if ch.get("is_active") else "  (inactive)"
                print(
                    f"  pk={str(pk):<5} catalog={str(ch.get('catalog')):<6} "
                    f"type={str(ct):<14} {ch.get('name')}{live}"
                )
        except OmnitronError as e:
            print(f"  could not read channels: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
