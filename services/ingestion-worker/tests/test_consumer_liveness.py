"""Coverage for issue #109: the consumer loop's failure modes are contained
and observable.

Two properties, both about the same thing -- the consumer is this service's
entire purpose, so "the consumer stopped" must not look like "the service is
fine":

1. Nothing raised while handling one message may unwind consume_forever.
2. /health reports the consumer's state, not a literal, so a dead consumer
   fails a probe instead of serving 200s while the queue backs up.

Plus the redelivery bound: a message that keeps failing is retried a fixed
number of times with backoff, then dropped and recorded, rather than
redelivered forever with no delay.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app import main as worker_main
from app import processing


class _Metadata:
    def __init__(self, num_delivered: int):
        self.num_delivered = num_delivered


class _Msg:
    """Records which JetStream disposition the loop chose for a message."""

    def __init__(self, data: bytes, num_delivered: int = 1, headers: dict | None = None):
        self.data = data
        self.headers = headers  # #134: nats-py's Msg always has this attribute
        self.metadata = _Metadata(num_delivered)
        self.acked = False
        self.termed = False
        self.nak_delay: float | None = None

    async def ack(self):
        self.acked = True

    async def term(self):
        self.termed = True

    async def nak(self, delay=None):
        self.nak_delay = delay


@pytest.fixture(autouse=True)
def _reset_status():
    processing.STATUS = processing.ConsumerStatus()
    yield
    processing.STATUS = processing.ConsumerStatus()


class TestMalformedPayload:
    async def test_unparseable_document_id_is_dropped_not_raised(self, monkeypatch):
        # Previously `uuid.UUID(msg.data.decode())` sat outside
        # process_document's error handling, so this raised ValueError out of
        # the fetch loop and killed the consumer.
        called = False

        async def _never(*_a, **_k):
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(processing, "process_document", _never)
        msg = _Msg(b"not-a-uuid")

        await processing._handle_message(msg)

        assert msg.termed is True
        assert msg.acked is False
        assert called is False, "a payload we can't parse must not reach processing"

    async def test_undecodable_bytes_are_also_dropped(self, monkeypatch):
        monkeypatch.setattr(processing, "process_document", None)
        msg = _Msg(b"\xff\xfe\x00")

        await processing._handle_message(msg)

        assert msg.termed is True


class TestRedeliveryBound:
    async def _handle(self, monkeypatch, *, terminal: bool, num_delivered: int) -> _Msg:
        async def _result(_doc_id):
            return terminal

        monkeypatch.setattr(processing, "process_document", _result)
        monkeypatch.setattr(processing, "_mark_undeliverable", lambda *_a: None)
        msg = _Msg(str(uuid.uuid4()).encode(), num_delivered=num_delivered)
        await processing._handle_message(msg)
        return msg

    async def test_terminal_outcome_is_acked(self, monkeypatch):
        msg = await self._handle(monkeypatch, terminal=True, num_delivered=1)

        assert msg.acked is True
        assert msg.nak_delay is None
        assert processing.STATUS.processed == 1

    async def test_transient_failure_is_naked_with_a_delay(self, monkeypatch):
        # nak() with no delay redelivers immediately, which is what turned a
        # persistently-failing message into a hot loop.
        msg = await self._handle(monkeypatch, terminal=False, num_delivered=1)

        assert msg.nak_delay == processing.NAK_BACKOFF_SECONDS
        assert msg.termed is False
        assert processing.STATUS.processed == 0

    async def test_final_attempt_is_terminated_not_redelivered(self, monkeypatch):
        msg = await self._handle(
            monkeypatch,
            terminal=False,
            num_delivered=processing.MAX_DELIVERY_ATTEMPTS,
        )

        assert msg.termed is True
        assert msg.nak_delay is None

    async def test_exhausted_message_marks_the_document_failed(self, monkeypatch):
        """Otherwise the row is stranded at `processing` with nothing to
        explain it, and the uploader never gets an FR-8 status."""
        recorded: list[tuple] = []

        async def _transient(_doc_id):
            return False

        monkeypatch.setattr(processing, "process_document", _transient)
        monkeypatch.setattr(
            processing, "_mark_undeliverable", lambda doc_id, n: recorded.append((doc_id, n))
        )
        doc_id = uuid.uuid4()

        await processing._handle_message(
            _Msg(str(doc_id).encode(), num_delivered=processing.MAX_DELIVERY_ATTEMPTS)
        )

        assert recorded == [(doc_id, processing.MAX_DELIVERY_ATTEMPTS)]

    async def test_consumer_config_caps_delivery_server_side_too(self):
        """The nak-side bound only helps while this worker is the one being
        redelivered to; max_deliver is what stops JetStream handing the same
        message to a fresh replica forever."""
        assert processing.MAX_DELIVERY_ATTEMPTS > 1
        assert processing.NAK_BACKOFF_SECONDS > 0


class TestHealthReflectsTheConsumer:
    def _health(self):
        return worker_main.health()

    def test_health_is_degraded_before_the_consumer_starts(self):
        payload = self._health()

        assert payload.status_code == 503

    def test_health_is_ok_while_the_consumer_runs(self):
        processing.STATUS.running = True

        payload = self._health()

        assert payload["status"] == "ok"
        assert payload["consumer"]["running"] is True

    def test_health_reports_the_exception_type_but_not_its_message(self):
        """/health takes no auth and is published to the host in Compose
        (8004:8004), so the payload names the failure mode without handing a
        caller internal broker hostnames or stream names from str(exc)."""
        processing.STATUS.running = False
        processing.STATUS.stopped_reason = "NoServersError"

        resp = self._health()

        assert resp.status_code == 503
        assert b"NoServersError" in resp.body
        assert b"stopped_reason" in resp.body


class TestConsumeForeverContainment:
    async def test_a_raising_message_handler_does_not_stop_the_loop(self, monkeypatch):
        """The whole point: one bad message is logged and skipped, and the
        loop keeps polling."""
        handled = []

        async def _boom(msg):
            handled.append(msg)
            if len(handled) == 1:
                raise RuntimeError("boom")
            raise asyncio.CancelledError  # stop the test's loop on the second pass

        monkeypatch.setattr(processing, "_handle_message", _boom)
        monkeypatch.setattr(processing, "ensure_stream", _noop_async)
        monkeypatch.setattr(processing, "get_nats_connection", _fake_nats(_Msg(b"x")))

        with pytest.raises(asyncio.CancelledError):
            await processing.consume_forever()

        assert len(handled) == 2, "loop stopped after the first raising message"
        assert processing.STATUS.running is False
        assert processing.STATUS.stopped_reason == "cancelled"

    async def test_status_records_an_unexpected_exit(self, monkeypatch):
        async def _explode(*_a, **_k):
            raise RuntimeError("nats gone")

        monkeypatch.setattr(processing, "get_nats_connection", _explode)

        with pytest.raises(RuntimeError):
            await processing.consume_forever()

        assert processing.STATUS.running is False
        # Type, not message -- see ConsumerStatus.stopped_reason.
        assert processing.STATUS.stopped_reason == "RuntimeError"
        assert "nats gone" not in str(processing.STATUS.as_dict())


async def _noop_async(*_a, **_k):
    return None


def _fake_nats(msg):
    class _Sub:
        async def fetch(self, _batch, timeout=None):
            return [msg]

    class _JS:
        async def pull_subscribe(self, *_a, **_k):
            return _Sub()

    class _NC:
        def jetstream(self):
            return _JS()

    async def _connect(*_a, **_k):
        return _NC()

    return _connect
