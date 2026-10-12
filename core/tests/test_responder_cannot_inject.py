"""A machine that cannot play a peer's input does not say it accepts being driven (WIRE.md section 4's
`accepts`), whatever `allow_drive` says: a Linux desktop with no way in for another machine's keys
would otherwise take a peer's keyboard and do nothing with it. Over real sockets on loopback, as
test_responder_ownership.py."""

import unittest

from core import protocol
from core.tests.responder_harness import B, C, Initiator, wait_for
from core.tests.test_responder_ownership import ACCEPT, Case


class Able:
    """An injector's `available`, as a platform part answers it: a plain read."""

    def __init__(self, machine, able):
        self.able = able
        self.asked = 0
        machine.injector.available = self

    def __call__(self):
        self.asked += 1
        if isinstance(self.able, BaseException):
            raise self.able
        return self.able


class CannotInject(Case):
    def welcome(self, peer=B):
        link = Initiator(self.machine, peer)
        self.addCleanup(link.close)
        welcome = link.handshake()
        self.assertTrue(wait_for(lambda: peer in self.machine.responder.links()))
        return link, welcome

    def test_the_welcome_says_it_does_not_accept_and_a_take_is_refused(self):
        Able(self.machine, False)
        b, welcome = self.welcome()
        self.assertIs(welcome["accepts"], False)
        self.refused(b, "not_allowed")
        self.assertIsNone(self.machine.responder.owner)
        # The link stays, for the clipboard's and the settings' sake.
        self.assertFalse(b.closed.is_set())

    def test_an_injector_that_does_not_say_is_one_that_can(self):
        # The Windows, Mac and X11 injectors have no `available`: nothing changes for them.
        self.assertFalse(hasattr(self.machine.injector, "available"))
        b, welcome = self.welcome()
        self.assertIs(welcome["accepts"], True)
        self.own(b)

    def test_allow_drive_off_still_refuses_when_the_injector_can(self):
        Able(self.machine, True)
        self.peer_entry(B)["allow_drive"] = False
        b, welcome = self.welcome()
        self.assertIs(welcome["accepts"], False)
        self.refused(b, "not_allowed")

    def test_becoming_able_is_announced_and_the_peer_may_then_drive(self):
        able = Able(self.machine, False)
        b, welcome = self.welcome()
        self.assertIs(welcome["accepts"], False)
        able.able = True
        self.machine.responder.peers_changed()
        self.assertEqual(b.expect("accepts"), {"accepts": True})
        self.own(b)

    def test_becoming_unable_sends_the_owner_home_and_tells_every_peer(self):
        able = Able(self.machine, True)
        b, _ = self.welcome(B)
        c, _ = self.welcome(C)
        route = self.own(b)
        b.key("keydown", "shift")
        self.assertTrue(wait_for(lambda: ("key", "shift", True) in self.machine.injected()))
        able.able = False
        self.machine.responder.peers_changed()
        self.assertEqual(b.expect("accepts"), {"accepts": False})
        self.assertEqual(c.expect("accepts"), {"accepts": False})
        self.assertEqual(b.expect("refuse"), {"route": route, "why": "not_allowed"})
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.refused(c, "not_allowed")

    def test_nothing_is_announced_when_nothing_changed(self):
        Able(self.machine, True)
        b, _ = self.welcome()
        for _ in range(3):
            self.machine.responder.peers_changed()
        self.assertIsNone(b.expect("accepts", timeout=0.3))

    def test_an_injector_that_fails_to_say_is_taken_as_able(self):
        # Refusing every peer because a question raised would be the worse failure.
        Able(self.machine, RuntimeError("broken"))
        with self.assertLogs("core.receiver", "ERROR"):
            b, welcome = self.welcome()
        self.assertIs(welcome["accepts"], True)


if __name__ == "__main__":
    unittest.main()
