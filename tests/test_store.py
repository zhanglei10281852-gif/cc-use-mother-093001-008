import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.events import Event
from district_works.store import EventStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_events_persist_and_replay_in_order(self):
        store = EventStore(self.path)
        e1 = store.append("A", {"x": 1})
        e2 = store.append("B", {"x": 2})
        self.assertEqual([e1.seq, e2.seq], [0, 1])

        store2 = EventStore(self.path)
        types = [e.type for e in store2.all_events()]
        self.assertEqual(types, ["A", "B"])
        # 序号在重放后继续
        e3 = store2.append("C", {})
        self.assertEqual(e3.seq, 2)

    def test_command_receipt_is_durable(self):
        store = EventStore(self.path)
        store.append("DID", {"ok": True}, "cmd-9")
        # 服务层将完整回执随 COMMAND_RECEIPTED 事件落盘，重启重放后
        # 同一 command_id 的重复提交返回同一份回执
        store.append("COMMAND_RECEIPTED",
                     {"receipt": {"command_id": "cmd-9", "answer": 42}},
                     "cmd-9")
        store2 = EventStore(self.path)
        receipt = store2.receipt_for("cmd-9")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["answer"], 42)

    def test_event_roundtrip_json(self):
        e = Event(seq=7, type="T", payload={"a": "中文"}, id="E-7")
        line = e.to_line()
        self.assertEqual(Event.from_line(line).payload["a"], "中文")


if __name__ == "__main__":
    unittest.main()
