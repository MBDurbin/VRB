"""Non-blocking publishing onto the rig's bounded inter-process queues."""
from queue import Empty, Full


def put_latest(q, item):
    """Queue `item`, dropping the oldest queued item if there is no room. Never blocks.

    For queues where only the newest item matters: DAQ packets and telemetry. A
    packet is worth nothing once a newer one exists.

    The pattern this replaces -- `if q.full(): q.get()` then `q.put(item)` --
    could block in both calls. The consumer can drain the queue between full()
    and get(), and get() then waits forever for an item that never comes. In the
    logic process that froze the safety loop: no trips, no STOP handling, no
    heartbeats.

    With a multiprocessing.Queue even the non-blocking calls can briefly
    disagree: get_nowait() can raise Empty while the producer's own puts are
    still being flushed by its feeder thread, so making room can fail. This item
    is then dropped and the next one tries again. Returns True if it was queued.
    """
    try:
        q.put_nowait(item)
        return True
    except Full:
        pass
    try:
        q.get_nowait()
    except Empty:
        pass
    try:
        q.put_nowait(item)
        return True
    except Full:
        return False
