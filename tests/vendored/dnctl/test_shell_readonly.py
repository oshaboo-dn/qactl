"""``cli shell`` read-only gating (#56).

``run start shell`` exec is destructive by default, but provably read-only
inspection (grep / ps / cat / ldd ...) should run without the ``--yes``
gate. The classifier is fail-closed: anything it can't prove safe keeps the
gate. These tests pin that boundary — no device traffic.
"""

from __future__ import annotations

import pytest

from dnctl.cli.core.shell_exec import is_read_only_shell


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -lE 'libasan|libubsan' /proc/[0-9]*/maps",
        "ps -eo pid,vsz,comm --sort=-vsz",
        "ps -eo pid,vsz,comm --sort=-vsz | head",
        "cat /proc/1557/environ",
        "ldd /usr/bin/bgpd",
        "/usr/bin/grep foo /tmp/x",            # absolute path resolves to grep
        "LANG=C grep foo bar",                  # leading env assignment skipped
        "find /proc -name maps",
        "grep -lE 'x' a/maps && wc -l",         # both segments read-only
    ],
)
def test_read_only_commands_need_no_yes(cmd):
    assert is_read_only_shell([cmd]) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/x",                        # unknown (mutating) binary
        "echo hi > /tmp/x",                     # output redirection
        "echo hi >> /tmp/x",                    # append redirection
        "cat $(which bgpd)",                    # command substitution
        "cat `which bgpd`",                     # backtick substitution
        "grep foo a && rm b",                   # one mutating segment poisons it
        "find /tmp -name x -delete",            # find with a write action
        "find / -exec rm {} ;",                 # find -exec runs a command
        "tee /tmp/x",                           # tee writes
        "ip link set eth0 down",                # ip can mutate
        "sed -i s/a/b/ f",                      # sed -i writes in place
    ],
)
def test_write_commands_keep_the_gate(cmd):
    assert is_read_only_shell([cmd]) is False


def test_string_input_accepted():
    assert is_read_only_shell("ps aux") is True


def test_empty_is_not_read_only():
    # Nothing to prove safe -> fail closed (the command itself is rejected
    # upstream, but the gate must not be silently dropped).
    assert is_read_only_shell([]) is False
    assert is_read_only_shell(["   "]) is False
