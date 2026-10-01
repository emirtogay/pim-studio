"""Omnitron API access. **Reads freely; writes through one narrow door.**

Decided 2026-08-14 (vault, `05 - Decisions Log`) that this module never writes
to Omnitron; revised 2026-09-07 to one scoped write: `OmnitronClient.post`,
for the product upsert alone, shut unless `OMNITRON_WRITES_ENABLED` is set,
and refusing any path, field, attribute key or locale outside what the
registry allows for the market being written (`WriteScopeViolation`).

That is enforced here rather than remembered. There is still no `patch`, `put`
or `delete`, and the one private request method raises `ReadOnlyViolation` on
any verb other than GET unless `post` has vouched for the call. The login POST
remains its own private path that cannot be handed an arbitrary URL.

The reasoning, from reading the API docs: `is_active: false` drops a product
from every channel at once; the integration image upsert deletes whatever the
request omits and returns 201 regardless; an `AttributeValue` in use cannot be
edited, so a bad write is not simply undone. A client that cannot reach those
endpoints is a better guarantee than a rule.
"""

from .client import OmnitronClient, OmnitronError, ReadOnlyViolation, WriteScopeViolation

__all__ = ["OmnitronClient", "OmnitronError", "ReadOnlyViolation", "WriteScopeViolation"]
