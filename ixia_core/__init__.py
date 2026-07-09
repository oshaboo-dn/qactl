"""Back-compat shim — moved to `qactl.ixia.core` (ixia consolidation 2026-07-09).
Kept so lingering `import ixia_core` keeps resolving. Remove once unused.
"""
import sys
import qactl.ixia.core as _pkg

sys.modules[__name__] = _pkg
