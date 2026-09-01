"""SPEC-LINE-PORTAL.md (admin web portal, branch `line-version`): the
tailnet-only admin web portal package -- a SECOND aiohttp listener
(`server.py`), the identity gate (`security.py`), the shared HTML shell +
render helpers (`layout.py`), and process-lifetime runtime stats
(`stats.py`). The four page modules (STATUS/USERS/AUDIT/QUOTA) land here
as sibling files (`status.py`/`users.py`/`audit.py`/`quota.py`) in a later,
parallel pass -- this package is empty of page content until then.

Nothing here is imported anywhere unless `config.portal.enabled` is
checked first (`core/app.py`'s own gate, R-SEC-1) -- importing this
package is always safe (no side effects at import time), but constructing
`PortalServer`/installing `RingBufferHandler` must stay conditional.
"""

from __future__ import annotations
