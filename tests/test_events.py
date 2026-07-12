import asyncio

from engine.events import EventBus


def test_publish_reaches_all_subscribers():
    async def main():
        bus = EventBus()
        q1, q2 = bus.subscribe("run-a"), bus.subscribe("run-a")
        other = bus.subscribe("run-b")
        bus.publish("run-a", {"id": 1, "type": "turn", "payload": {}})
        assert q1.get_nowait()["id"] == 1
        assert q2.get_nowait()["id"] == 1
        assert other.empty()  # run lain nggak kena

    asyncio.run(main())


def test_publish_without_subscribers_is_noop():
    async def main():
        EventBus().publish("ghost", {"id": 1})

    asyncio.run(main())


def test_unsubscribe( ):
    async def main():
        bus = EventBus()
        q = bus.subscribe("r")
        assert bus.subscriber_count("r") == 1
        bus.unsubscribe("r", q)
        assert bus.subscriber_count("r") == 0
        bus.publish("r", {"id": 1})
        assert q.empty()

    asyncio.run(main())


def test_slow_subscriber_drops_not_blocks():
    async def main():
        bus = EventBus()
        q = bus.subscribe("r")
        for i in range(1500):  # > QUEUE_SIZE
            bus.publish("r", {"id": i})
        assert q.qsize() == 1000  # sisanya ke-drop, publish nggak pernah block

    asyncio.run(main())
