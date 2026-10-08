"""
put_latest(): publishing onto a bounded queue without ever blocking.

The logic process, the DAQ and the SIL plant all publish this way. The old
pattern, `if q.full(): q.get()` then `q.put(item)`, could block forever, and in
the logic process that froze every trip, the E-STOP and the heartbeat.
"""
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from queue_util import put_latest


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class DrainedAfterFull:
    """Full on the first put, but empty by the time anything tries to get.

    The consumer drained it in between, which is the race that left the old
    blocking get() waiting forever.
    """
    def __init__(self):
        self.items = []
        self.full_once = True

    def put_nowait(self, item):
        if self.full_once:
            self.full_once = False
            raise queue.Full
        self.items.append(item)

    def get_nowait(self):
        raise queue.Empty


class Unflushed:
    """No room, and none can be made: a multiprocessing.Queue whose own puts
    have not yet been flushed by its feeder thread."""
    def put_nowait(self, item):
        raise queue.Full

    def get_nowait(self):
        raise queue.Empty


def test_queues_when_there_is_room():
    q = queue.Queue(maxsize=3)
    assert put_latest(q, 1) is True
    assert drain(q) == [1]


def test_full_queue_drops_the_oldest_and_keeps_the_newest():
    q = queue.Queue(maxsize=3)
    for i in range(10):
        put_latest(q, i)
    assert drain(q) == [7, 8, 9]


def test_queue_drained_mid_publish_still_takes_the_item():
    q = DrainedAfterFull()
    assert put_latest(q, "packet") is True
    assert q.items == ["packet"]


def test_no_room_drops_this_item_instead_of_blocking():
    # If this hangs, a blocking call is back.
    assert put_latest(Unflushed(), "packet") is False
