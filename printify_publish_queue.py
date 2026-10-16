"""printify-publish-queue — a hardened publish flow for the Printify API.

Printify's publish endpoint returns HTTP 200 immediately and then syncs to the
sales channel (Shopify, Etsy, ...) through an internal queue. That queue has
three distinct failure modes, and the naive response to each one makes things
worse. This module encodes the safe state machine:

  Rule 1: wait at least 5 minutes (default 7) before judging a publish.
  Rule 2: NEVER re-publish a product that is still locked after the wait.
          Locked means queued. A retry stacks a second publish -> duplicates.
  Rule 3: a genuinely silent failure (unlocked, no external id, nothing on the
          channel) is recovered by DELETE + re-create, never by re-publishing
          the same product.
  Rule 4: bulk publishes are serialized with a cooldown between products.
          Parallel triggers exhaust the internal queue and cause both
          duplicates and silent failures.
  Rule 5: trust the TARGET channel over Printify's external-id cache. Verify
          by querying the channel directly; they can disagree.

There is one more self-inflicted failure mode: two products with identical
titles make title-based verification ambiguous (and Etsy treats them as
duplicate listings). `publish_many` refuses such a batch up front.

The client takes injectable `session`, `sleep`, and `monotonic` so the whole
state machine is unit-testable without the network or real time. See
test_publish_queue.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Sequence

API_BASE = "https://api.printify.com/v1"

# What gets synced on publish. Printify wants explicit booleans.
DEFAULT_PUBLISH_BODY = {
    "title": True,
    "description": True,
    "images": True,
    "variants": True,
    "tags": True,
}


class Outcome(Enum):
    SUCCEEDED = "succeeded"        # external id present (or verified on channel)
    STILL_QUEUED = "still_queued"  # locked after the wait window: leave it alone
    RECREATED = "recreated"        # silent failure -> deleted, re-created, published
    FAILED = "failed"              # out of safe moves; needs a human


class TitleCollisionError(ValueError):
    """Two products in one batch share a title. Refuse before any API call."""


@dataclass
class PublishResult:
    product_id: str
    title: str
    outcome: Outcome
    external_id: Optional[str] = None
    publish_calls: int = 1
    notes: list = field(default_factory=list)


class PublishQueue:
    """Serialized, failure-aware publisher for one Printify shop.

    Parameters
    ----------
    token, shop_id : Printify API token and shop id.
    session : requests.Session-compatible object (injectable for tests).
    first_wait_s : how long to poll before judging a publish (>= 300; default 420).
    poll_interval_s : seconds between status polls.
    cooldown_s : pause between products in publish_many (Rule 4).
    channel_has_listing : optional callback(title) -> bool that checks the
        TARGET channel directly (Rule 5). Without it, silent failures cannot
        be distinguished from cache disagreement, so recovery is skipped.
    recreate_product : optional callback(product_id, title) -> new_product_id
        that re-creates the product from source data (image upload + create).
        Required for the DELETE + re-create recovery (Rule 3).
    """

    def __init__(
        self,
        token: str,
        shop_id: str,
        *,
        session=None,
        first_wait_s: int = 420,
        poll_interval_s: int = 60,
        cooldown_s: int = 75,
        channel_has_listing: Optional[Callable[[str], bool]] = None,
        recreate_product: Optional[Callable[[str, str], str]] = None,
        sleep: Callable[[float], None] = None,
        monotonic: Callable[[], float] = None,
    ):
        if first_wait_s < 300:
            raise ValueError(
                "first_wait_s must be >= 300 (Rule 1: a publish is not stuck "
                "before 5 minutes; judging early is how duplicates happen)"
            )
        if session is None:
            import requests  # deferred so tests never need it

            session = requests.Session()
        import time as _time

        self.token = token
        self.shop_id = shop_id
        self.session = session
        self.first_wait_s = first_wait_s
        self.poll_interval_s = poll_interval_s
        self.cooldown_s = cooldown_s
        self.channel_has_listing = channel_has_listing
        self.recreate_product = recreate_product
        self.sleep = sleep or _time.sleep
        self.monotonic = monotonic or _time.monotonic

    # ----- HTTP helpers ------------------------------------------------------

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _url(self, path: str) -> str:
        return f"{API_BASE}/shops/{self.shop_id}{path}"

    def _get_product(self, product_id: str) -> dict:
        r = self.session.get(self._url(f"/products/{product_id}.json"), headers=self._headers())
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: dict) -> None:
        r = self.session.post(self._url(path), headers=self._headers(), json=json)
        r.raise_for_status()

    def _delete_product(self, product_id: str) -> None:
        r = self.session.delete(self._url(f"/products/{product_id}.json"), headers=self._headers())
        r.raise_for_status()

    # ----- the state machine --------------------------------------------------

    def publish(self, product_id: str, title: str, _is_retry: bool = False) -> PublishResult:
        """Publish one product and see it through to a safe verdict."""
        result = PublishResult(product_id=product_id, title=title, outcome=Outcome.FAILED)

        self._post(f"/products/{product_id}/publish.json", json=dict(DEFAULT_PUBLISH_BODY))

        # Poll inside the wait window (Rule 1).
        deadline = self.monotonic() + self.first_wait_s
        product: dict = {}
        while self.monotonic() < deadline:
            self.sleep(self.poll_interval_s)
            product = self._get_product(product_id)
            external_id = (product.get("external") or {}).get("id")
            if external_id:
                self._ack_success(product_id, product)
                result.outcome = Outcome.RECREATED if _is_retry else Outcome.SUCCEEDED
                result.external_id = external_id
                return result

        # Window exhausted. Locked = still queued. Walk away (Rule 2).
        if product.get("is_locked"):
            result.outcome = Outcome.STILL_QUEUED
            result.notes.append(
                "locked after wait window: publish is queued, not stuck. "
                "Re-publishing now would create a duplicate. Check again later."
            )
            return result

        # Unlocked, no external id. Maybe the cache is lying (Rule 5).
        if self.channel_has_listing is not None and self.channel_has_listing(title):
            result.outcome = Outcome.SUCCEEDED
            result.notes.append(
                "listing exists on the sales channel but Printify's external-id "
                "cache is empty. Treating as succeeded; do not re-publish."
            )
            return result

        # Genuine silent failure. DELETE + re-create is the only safe recovery (Rule 3).
        if _is_retry or self.recreate_product is None:
            result.notes.append(
                "silent failure and no safe recovery available "
                + ("(already retried once)" if _is_retry else "(no recreate_product callback)")
                + ". Needs a human."
            )
            return result

        self._post(
            f"/products/{product_id}/publishing_failed.json",
            json={"reason": "silent failure: cancelling before delete + re-create"},
        )
        self._delete_product(product_id)
        self.sleep(60)  # let the cancellation propagate before creating a sibling
        new_id = self.recreate_product(product_id, title)
        retry = self.publish(new_id, title, _is_retry=True)
        retry.publish_calls += result.publish_calls
        retry.notes = result.notes + [f"recreated {product_id} -> {new_id}"] + retry.notes
        return retry

    def publish_many(self, items: Sequence[tuple]) -> list:
        """Serialized batch publish. items = [(product_id, title), ...].

        Refuses duplicate titles up front: title-based verification (Rule 5)
        is ambiguous against a batch that contains the same title twice, and
        on Etsy duplicate titles are an algorithm penalty besides.
        """
        titles = [t for _, t in items]
        dupes = {t for t in titles if titles.count(t) > 1}
        if dupes:
            raise TitleCollisionError(
                f"duplicate titles in batch: {sorted(dupes)}. Differentiate "
                "titles (e.g. a style or colorway suffix) before publishing."
            )
        results = []
        for i, (product_id, title) in enumerate(items):
            if i > 0:
                self.sleep(self.cooldown_s)  # Rule 4
            results.append(self.publish(product_id, title))
        return results

    # ----- internals ----------------------------------------------------------

    def _ack_success(self, product_id: str, product: dict) -> None:
        external = product.get("external") or {}
        self._post(
            f"/products/{product_id}/publishing_succeeded.json",
            json={"external": {"id": external.get("id"), "handle": external.get("handle", "")}},
        )
