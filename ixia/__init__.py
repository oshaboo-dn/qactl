"""Back-compat shim — moved to `qactl.ixia.client` (ixia consolidation 2026-07-09).
Kept so lingering `import ixia` keeps resolving. Remove once unused.
"""
import sys
import qactl.ixia.client as _pkg

sys.modules[__name__] = _pkg
