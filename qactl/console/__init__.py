"""``qactl console`` — open a device's serial console via the lab terminal servers.

Top-level group: the console server + port are resolved from Device42 behind
the scenes (or given manually), but reaching a console is a device action, not
a Device42 query — so it lives here, not under ``qactl d42`` (which is reserved
for things read directly out of the CMDB).
"""
