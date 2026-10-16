"""Unit tests for the publish state machine. No network, no real time.

Each test simulates one of the documented failure modes through a fake
session and a fake clock, then asserts the machine made ONLY the safe moves.
The most important assertions are the negative ones: exactly one publish call
in the still-queued case, zero API calls before a title-collision refusal.

Run: python3 -m unittest test_publish_queue -v
"""

import unittest

from printify_publish_queue import (
    Outcome,
    PublishQueue,
    TitleCollisionError,
)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeSession:
    """Scripted Printify API. product_states maps product_id -> list of GET
    payloads, consumed one per poll (last one repeats)."""

    def __init__(self, product_states):
        self.product_states = {k: list(v) for k, v in product_states.items()}
        self.calls = []  # (method, url) tuples, in order

    def _product_id(self, url):
        return url.rsplit("/products/", 1)[1].split(".json")[0].split("/")[0]

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        states = self.product_states[self._product_id(url)]
        payload = states.pop(0) if len(states) > 1 else states[0]
        return FakeResponse(payload)

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return FakeResponse()

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return FakeResponse()

    def count(self, method, fragment):
        return sum(1 for m, u in self.calls if m == method and fragment in u)


def make_queue(session, clock, **kw):
    return PublishQueue(
        "token",
        "shop1",
        session=session,
        first_wait_s=300,
        poll_interval_s=60,
        cooldown_s=75,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kw,
    )


LOCKED = {"is_locked": True, "external": None}
DONE = {"is_locked": False, "external": {"id": "ext-1", "handle": "h"}}
SILENT = {"is_locked": False, "external": None}


class TestHappyPath(unittest.TestCase):
    def test_external_id_on_second_poll_succeeds_and_acks(self):
        session = FakeSession({"p1": [LOCKED, DONE]})
        clock = FakeClock()
        result = make_queue(session, clock).publish("p1", "Sunset Print Mug")
        self.assertEqual(result.outcome, Outcome.SUCCEEDED)
        self.assertEqual(result.external_id, "ext-1")
        self.assertEqual(session.count("POST", "/publish.json"), 1)
        self.assertEqual(session.count("POST", "/publishing_succeeded.json"), 1)


class TestFailureA_StillQueued(unittest.TestCase):
    def test_locked_after_window_means_walk_away(self):
        session = FakeSession({"p1": [LOCKED]})
        clock = FakeClock()
        result = make_queue(session, clock).publish("p1", "Sunset Print Mug")
        self.assertEqual(result.outcome, Outcome.STILL_QUEUED)
        # THE assertion: exactly one publish call. A second one = duplicates.
        self.assertEqual(session.count("POST", "/publish.json"), 1)
        self.assertEqual(session.count("DELETE", "/products/"), 0)


class TestFailureB_SilentFailure(unittest.TestCase):
    def test_channel_listing_found_means_cache_disagreement_not_failure(self):
        session = FakeSession({"p1": [SILENT]})
        clock = FakeClock()
        q = make_queue(session, clock, channel_has_listing=lambda title: True)
        result = q.publish("p1", "Sunset Print Mug")
        self.assertEqual(result.outcome, Outcome.SUCCEEDED)
        self.assertEqual(session.count("POST", "/publish.json"), 1)
        self.assertEqual(session.count("DELETE", "/products/"), 0)

    def test_true_silent_failure_recovers_via_delete_and_recreate(self):
        session = FakeSession({"p1": [SILENT], "p2": [DONE]})
        clock = FakeClock()
        recreated = []

        def recreate(old_id, title):
            recreated.append(old_id)
            return "p2"

        q = make_queue(
            session,
            clock,
            channel_has_listing=lambda title: False,
            recreate_product=recreate,
        )
        result = q.publish("p1", "Sunset Print Mug")
        self.assertEqual(result.outcome, Outcome.RECREATED)
        self.assertEqual(result.external_id, "ext-1")
        self.assertEqual(recreated, ["p1"])
        # Old product: cancelled then deleted. Never re-published.
        self.assertEqual(session.count("POST", "p1/publishing_failed.json"), 1)
        self.assertEqual(session.count("DELETE", "/products/p1"), 1)
        self.assertEqual(session.count("POST", "p1/publish.json"), 1)
        self.assertEqual(session.count("POST", "p2/publish.json"), 1)

    def test_silent_failure_without_recovery_callback_fails_safe(self):
        session = FakeSession({"p1": [SILENT]})
        clock = FakeClock()
        q = make_queue(session, clock, channel_has_listing=lambda title: False)
        result = q.publish("p1", "Sunset Print Mug")
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertEqual(session.count("DELETE", "/products/"), 0)

    def test_recovery_does_not_loop(self):
        # The recreated product ALSO fails silently -> FAILED, no second recreate.
        session = FakeSession({"p1": [SILENT], "p2": [SILENT]})
        clock = FakeClock()
        calls = []
        q = make_queue(
            session,
            clock,
            channel_has_listing=lambda title: False,
            recreate_product=lambda old, t: calls.append(old) or "p2",
        )
        result = q.publish("p1", "Sunset Print Mug")
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertEqual(calls, ["p1"])  # exactly one recreation attempt


class TestFailureC_TitleCollision(unittest.TestCase):
    def test_duplicate_titles_refused_before_any_api_call(self):
        session = FakeSession({})
        clock = FakeClock()
        q = make_queue(session, clock)
        with self.assertRaises(TitleCollisionError):
            q.publish_many([("p1", "Painted Mug"), ("p2", "Painted Mug")])
        self.assertEqual(session.calls, [])


class TestSerialization(unittest.TestCase):
    def test_cooldown_between_products(self):
        session = FakeSession({"p1": [DONE], "p2": [DONE]})
        clock = FakeClock()
        q = make_queue(session, clock)
        results = q.publish_many([("p1", "Mug A"), ("p2", "Mug B")])
        self.assertEqual([r.outcome for r in results], [Outcome.SUCCEEDED] * 2)
        # one poll sleep per product (60) + one cooldown (75)
        self.assertEqual(clock.t, 60 + 75 + 60)


class TestGuardrails(unittest.TestCase):
    def test_wait_window_under_5_minutes_is_rejected(self):
        with self.assertRaises(ValueError):
            PublishQueue("t", "s", session=FakeSession({}), first_wait_s=120)


if __name__ == "__main__":
    unittest.main()
