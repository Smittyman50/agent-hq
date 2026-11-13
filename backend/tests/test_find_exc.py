"""_find_exc copes with exception chains that loop back on themselves."""

from __future__ import annotations

from app.runtime import Escalation, _find_exc


def test_finds_escalation_in_group_and_chain():
    esc = Escalation("too hard")
    group = ExceptionGroup("g", [ValueError("x"), esc])
    outer = RuntimeError("outer")
    outer.__cause__ = group
    assert _find_exc(outer, Escalation) is esc


def test_cyclic_chain_terminates():
    # Like fastmcp: a group whose member is caused by an error whose cause is the group.
    leaf = RuntimeError("Client failed to connect")
    group = ExceptionGroup("g", [leaf])
    leaf.__cause__ = group
    top = RuntimeError("top")
    top.__context__ = leaf
    assert _find_exc(top, Escalation) is None
