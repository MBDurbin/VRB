"""
Unit tests for the GUI -> logic command dispatch path.

The E-STOP button rides this path, so it must never block and never lose a STOP.
These use a plain queue.Queue (same bounded semantics and same Full/Empty
exceptions as multiprocessing.Queue) so no GUI or subprocess is needed.
"""
import queue
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui_layout import send_command_nonblocking


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class TestNonBlockingDispatch:
    def test_normal_send_enqueues(self):
        q = queue.Queue(maxsize=10)
        assert send_command_nonblocking(q, "ARM") is True
        assert drain(q) == ["ARM"]

    def test_does_not_block_when_full(self):
        # The original bug: .put() on a full queue blocks the GUI thread forever.
        # If this test hangs, the regression is back.
        q = queue.Queue(maxsize=3)
        for i in range(3):
            send_command_nonblocking(q, ("SET_LIMITS", i))
        assert q.full()

        assert send_command_nonblocking(q, "STOP") is True

    def test_estop_gets_through_a_full_queue(self):
        q = queue.Queue(maxsize=3)
        for i in range(3):
            send_command_nonblocking(q, ("SET_LIMITS", i))

        send_command_nonblocking(q, "STOP")
        assert "STOP" in drain(q)

    def test_full_queue_drops_oldest_not_newest(self):
        q = queue.Queue(maxsize=3)
        for i in range(3):
            send_command_nonblocking(q, ("SET_LIMITS", i))

        send_command_nonblocking(q, ("SET_LIMITS", 99))
        remaining = drain(q)

        assert ("SET_LIMITS", 0) not in remaining  # oldest evicted
        assert ("SET_LIMITS", 99) in remaining     # newest survived

    def test_queued_estop_is_never_evicted(self):
        # STOP sitting at the head must survive later spinbox spam.
        q = queue.Queue(maxsize=3)
        send_command_nonblocking(q, "STOP")
        send_command_nonblocking(q, ("SET_LIMITS", 1))
        send_command_nonblocking(q, ("SET_LIMITS", 2))
        assert q.full()

        # Spam more limit changes against the full queue.
        for i in range(10):
            send_command_nonblocking(q, ("SET_LIMITS", 100 + i))

        assert "STOP" in drain(q)

    def test_non_critical_dropped_when_it_would_evict_estop(self):
        q = queue.Queue(maxsize=1)
        send_command_nonblocking(q, "STOP")

        assert send_command_nonblocking(q, ("SET_LIMITS", 1)) is False
        assert drain(q) == ["STOP"]

    def test_estop_may_replace_a_queued_estop(self):
        # Redundant STOPs collapse -- same net effect, still a STOP pending.
        q = queue.Queue(maxsize=1)
        send_command_nonblocking(q, "STOP")

        assert send_command_nonblocking(q, "STOP") is True
        assert drain(q) == ["STOP"]

    def test_spinbox_spam_then_estop_still_arrives(self):
        # End-to-end shape of the reported hazard: rapid limit changes while the
        # logic process is stalled, followed by a panic E-STOP press.
        q = queue.Queue(maxsize=10)
        for i in range(200):
            send_command_nonblocking(q, ("SET_LIMITS", i))

        assert send_command_nonblocking(q, "STOP") is True
        assert "STOP" in drain(q)
