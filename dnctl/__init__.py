"""Back-compat shim — the ``dnctl`` package moved under ``qactl.dnctl``
(consolidation 2026-07-09). Aliases the old top-level name to the new
package so any lingering/dynamic ``import dnctl`` keeps working. In-repo
code and tests now import ``qactl.dnctl`` directly; remove this once no
external caller relies on the old name.
"""
import sys
import qactl.dnctl as _pkg

sys.modules[__name__] = _pkg
