"""Per-device named mutex registry.

Several tools share the DNOS *candidate configuration* on a given device
(``edit_config``, ``backup_device``, ``restore_device``,
``create_techsupport``). They mustn't run concurrently against the same
device or one transaction will overwrite another's candidate. This
module hands out a lazily-created ``threading.Lock`` per device key, so
in-flight tool calls serialise per-device while different devices stay
independent.

Lock keys are arbitrary strings — typically the resolved ``device``
alias or, when only a raw ``host`` is supplied, the host itself. The
caller picks the key; this module only owns the registry.
"""

from __future__ import annotations

import threading
from typing import Dict


_DEVICE_LOCKS: Dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def device_lock(key: str) -> threading.Lock:
    """Return the (lazily created) lock for ``key``.

    The registry itself is mutex-protected so concurrent first-touchers
    don't race to construct two different ``Lock`` instances under the
    same key.
    """
    with _REGISTRY_LOCK:
        lock = _DEVICE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DEVICE_LOCKS[key] = lock
        return lock
