"""Back-compat shim — moved to `qactl.ixia.ctl` (ixia consolidation 2026-07-09).
Kept so lingering `import ixiactl` keeps resolving. Remove once unused.
"""
import sys
import qactl.ixia.ctl as _pkg

sys.modules[__name__] = _pkg
