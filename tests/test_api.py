import os
import tempfile
import unittest

os.environ["LIGHTS_DB"] = os.path.join(tempfile.mkdtemp(), "test_api.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def abs_body(fixture="L1", channel=1, target=100, duration=1000, ms=0):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "abs", "start_ms": ms}


def ref_body(ref_id, edge="start", offset=0, fixture="L1", channel=1,
             target=100, duration=1000):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "ref",
            "ref_id": ref_id, "ref_edge": edge, "ref_offset": offset}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def setUp(self):
        # fresh empty project per test
        r = self.client.post("/api/projects", json={"name": "t"})
        self.pid = r.json()["id"]
        self.base = f"/api/projects/{self.pid}"

    def test_create_and_resolve(self):
        r = self.client.post(f"{self.base}/items", json=abs_body(ms=100, duration=200))
        self.assertEqual(r.status_code, 201, r.text)
        it = r.json()["items"][0]
        self.assertEqual(it["actual_start"], 100)
        self.assertEqual(it["actual_end"], 300)

    def test_missing_reference_rejected_and_unchanged(self):
        self.client.post(f"{self.base}/items", json=abs_body())
        before = self.client.get(self.base).json()
        r = self.client.post(f"{self.base}/items", json=ref_body(999))
        self.assertEqual(r.status_code, 400)
        self.assertIn("引用缺失", r.json()["detail"])
        after = self.client.get(self.base).json()
        self.assertEqual(len(after["items"]), 1)
        self.assertEqual(before, after)

    def test_cycle_rejected(self):
        a = self.client.post(f"{self.base}/items", json=abs_body(channel=1)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=ref_body(id_a, channel=2)).json()
        id_b = b["items"][-1]["id"]
        r = self.client.put(f"{self.base}/items/{id_a}",
                            json=ref_body(id_b, channel=1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("环", r.json()["detail"])
        # item a still absolute
        cur = self.client.get(self.base).json()["items"]
        self.assertEqual(cur[0]["start_mode"], "abs")

    def test_same_instant_rejected(self):
        self.client.post(f"{self.base}/items", json=abs_body(ms=50))
        r = self.client.post(f"{self.base}/items", json=abs_body(ms=50, target=20))
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时起步", r.json()["detail"])
        self.assertEqual(len(self.client.get(self.base).json()["items"]), 1)

    def test_delete_referenced_rejected(self):
        a = self.client.post(f"{self.base}/items", json=abs_body()).json()
        id_a = a["items"][0]["id"]
        self.client.post(f"{self.base}/items", json=ref_body(id_a, channel=2))
        r = self.client.delete(f"{self.base}/items/{id_a}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("引用", r.json()["detail"])
        self.assertEqual(len(self.client.get(self.base).json()["items"]), 2)

    def test_delete_then_ok(self):
        a = self.client.post(f"{self.base}/items", json=abs_body()).json()
        id_a = a["items"][0]["id"]
        r = self.client.delete(f"{self.base}/items/{id_a}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])

    def test_recalculate_after_edit(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(ms=0, duration=100)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=ref_body(id_a, "end", offset=10, channel=2)).json()
        id_b = b["items"][-1]["id"]
        self.assertEqual(b["items"][-1]["actual_start"], 110)
        r = self.client.put(f"{self.base}/items/{id_a}",
                            json=abs_body(ms=0, duration=250))
        self.assertEqual(r.status_code, 200)
        b_after = next(i for i in r.json()["items"] if i["id"] == id_b)
        self.assertEqual(b_after["actual_start"], 260)

    def test_eval_matches_engine(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=3, duration=10)).json()
        id_a = a["items"][0]["id"]
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=103, duration=100, ms=5))
        r = self.client.get(f"{self.base}/eval", params={"t": 4})
        ch = r.json()["channels"][0]
        self.assertEqual(ch["raw"], "6/5")
        self.assertEqual(ch["value"], 1)
        self.assertEqual(ch["active_item_id"], id_a)
        r2 = self.client.get(f"{self.base}/eval", params={"t": 5})
        ch2 = r2.json()["channels"][0]
        self.assertEqual(ch2["raw"], "3/2")
        self.assertEqual(ch2["value"], 2)

    def test_field_validation(self):
        r = self.client.post(f"{self.base}/items",
                             json=abs_body(target=256))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"{self.base}/items",
                             json=abs_body(duration=-1))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"{self.base}/items",
                             json={**abs_body(), "start_mode": "ref",
                                   "ref_id": 1, "ref_edge": "middle"})
        self.assertEqual(r.status_code, 422)

    def test_interrupted_flag(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=100, duration=1000)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=abs_body(target=0, duration=100, ms=500)).json()
        id_b = b["items"][-1]["id"]
        items = {i["id"]: i for i in b["items"]}
        self.assertEqual(items[id_a]["interrupted_by"], id_b)
        self.assertIsNone(items[id_b]["interrupted_by"])


if __name__ == "__main__":
    unittest.main()
