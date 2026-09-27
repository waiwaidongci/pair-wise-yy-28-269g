import json, os, sys, tempfile, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module
from database import ContinuityDB, DomainError, RevisionConflict


class PreviewCommitTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = ContinuityDB(self.path)
        self.producer = self.db.add_user("制片", "producer")
        self.continuity = self.db.add_user("场记", "continuity")
        self.reviewer = self.db.add_user("审片", "reviewer")
        self.production = self.db.create_production("测试影片", "非线性拍摄", self.producer)
        self.scene = self.db.add_scene(self.production, "S01", "雨夜", 1)
        self.s1 = self.db.add_shot(self.scene, "S01-01", 2, 1, "受伤后", self.continuity)
        self.s2 = self.db.add_shot(self.scene, "S01-02", 1, 2, "受伤前", self.continuity)
        self.injury = self.db.add_element(self.production, "手臂伤痕", "injury", "monotonic", "只能加重")
        self.db.set_element_state(self.s1, self.injury, "重度", 3, "", self.continuity)
        self.db.set_element_state(self.s2, self.injury, "轻度", 1, "", self.continuity)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _preview(self, shot, value, numeric):
        return self.db.preview_element_state(shot, self.injury, value, numeric, "试算备注", self.continuity)

    def test_preview_does_not_save_and_lists_diff(self):
        before = self.db.shot_info(self.s2)["shot"]["version"]
        trial = self._preview(self.s2, "重度", 3)
        self.assertFalse(trial["saved"])
        self.assertEqual(before, trial["revision"])
        self.assertEqual(1, trial["active_conflicts_before"])
        self.assertEqual(0, trial["active_conflicts_after"])
        self.assertEqual([], trial["conflicts_added"])
        self.assertEqual("regression", trial["conflicts_removed"][0]["kind"])
        # 受影响镜头：被编辑的 s2 与冲突另一端 s1
        codes = {a["shot_code"] for a in trial["affected_shots"]}
        self.assertEqual({"S01-01", "S01-02"}, codes)
        # 试算不保存：冲突仍在，状态仍旧
        self.assertEqual(1, len(self.db.list_conflicts(self.scene)))
        self.assertEqual("轻度", self.db.shot_info(self.s2)["states"][0]["state_value"])
        self.assertEqual(before, self.db.shot_info(self.s2)["shot"]["version"])

    def test_preview_flags_new_conflicts(self):
        # 先消除既有回退（3→3 无冲突），再试算把后一镜头改低：应新增 regression
        self.db.set_element_state(self.s2, self.injury, "重度", 3, "", self.continuity)
        self.assertEqual([], self.db.list_conflicts(self.scene))
        trial = self._preview(self.s2, "轻度", 1)
        self.assertEqual("regression", trial["conflicts_added"][0]["kind"])
        self.assertEqual([], trial["conflicts_removed"])
        self.assertEqual(0, trial["active_conflicts_before"])
        self.assertEqual(1, trial["active_conflicts_after"])

    def test_commit_writes_once_bumps_revision_and_records(self):
        trial = self._preview(self.s2, "重度", 3)
        result = self.db.commit_element_state(self.s2, self.injury, "重度", 3, "确认修改",
                                              self.continuity, trial["revision"])
        self.assertTrue(result["saved"])
        self.assertEqual(trial["revision"] + 1, result["revision"])
        self.assertEqual([], result["conflicts"])
        info = self.db.shot_info(self.s2)
        self.assertEqual(trial["revision"] + 1, info["shot"]["version"])
        rec = info["records"][0]
        self.assertEqual("轻度", rec["from_value"]); self.assertEqual("重度", rec["to_value"])
        self.assertEqual(1.0, rec["from_numeric"]); self.assertEqual(3.0, rec["to_numeric"])
        self.assertEqual(trial["revision"], rec["from_version"])
        self.assertEqual(result["revision"], rec["to_version"])
        self.assertEqual("确认修改", rec["note"])

    def test_commit_rejects_stale_revision_with_both_sides(self):
        trial = self._preview(self.s2, "中度", 2)
        # 别人先改过：修订号前进
        self.db.set_element_state(self.s2, self.injury, "轻度", 1.5, "同事先改", self.continuity)
        with self.assertRaises(RevisionConflict) as ctx:
            self.db.commit_element_state(self.s2, self.injury, "中度", 2, "确认修改",
                                         self.continuity, trial["revision"])
        payload = ctx.exception.payload
        self.assertEqual(trial["revision"], payload["expected_revision"])
        self.assertEqual(trial["revision"] + 1, payload["current_revision"])
        self.assertEqual("中度", payload["submitted"]["state_value"])
        self.assertEqual("轻度", payload["server_state"]["state_value"])
        self.assertEqual(1.5, payload["server_state"]["numeric_value"])
        # 被拒绝后状态与试算一致（未写入）
        self.assertEqual("轻度", self.db.shot_info(self.s2)["states"][0]["state_value"])

    def test_locked_shot_rejected_at_preview_and_commit(self):
        self.db.approve_exemption(self.db.list_conflicts(self.scene)[0]["id"],
                                  "闪回镜头中伤痕表现属于刻意叙事误差", self.reviewer)
        self.db.lock_shot(self.s1, self.continuity); self.db.lock_shot(self.s2, self.continuity)
        with self.assertRaisesRegex(DomainError, "锁定"):
            self.db.preview_element_state(self.s2, self.injury, "重度", 3, "", self.continuity)
        with self.assertRaisesRegex(DomainError, "锁定"):
            self.db.commit_element_state(self.s2, self.injury, "重度", 3, "", self.continuity, 2)

    def test_reviewer_has_no_access(self):
        with self.assertRaisesRegex(DomainError, "审片人员"):
            self.db.preview_element_state(self.s2, self.injury, "重度", 3, "", self.reviewer)

    def test_approved_plan_also_bumps_revision_and_records(self):
        before = self.db.shot_info(self.s2)["shot"]["version"]
        conflict = self.db.list_conflicts(self.scene)[0]
        plan = self.db.propose_adjustment(conflict["id"], "重度", 3, "将伤势调整到叙事顺序上的中间状态", self.continuity)
        self.db.review_adjustment(plan, True, self.reviewer, "通过")
        info = self.db.shot_info(self.s2)
        self.assertEqual(before + 1, info["shot"]["version"])
        self.assertEqual("重度", info["states"][0]["state_value"])
        self.assertTrue(any(r["to_value"] == "重度" for r in info["records"]))


class HttpPreviewTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        app_module.Handler.db = ContinuityDB(self.path)
        db = app_module.Handler.db
        db.seed_demo()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        app_module.Handler.db.close(); os.unlink(self.path)

    def _post(self, path, body):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as resp:
            return json.loads(resp.read())

    def test_http_preview_confirm_and_409(self):
        snap = self._get("/api/state")
        shot = next(s for s in snap["shots"] if s["shot_code"] == "S01-02")
        sid = shot["id"]
        continuity = next(u for u in snap["users"] if u["role"] == "continuity")["id"]
        production_id = snap["productions"][0]["id"]
        report = self._get("/api/productions/%d/continuity" % production_id)
        injury = report["elements"][0]["id"]
        body = {"element_id": injury, "state_value": "重度", "numeric_value": 3,
                "note": "http 试算", "user_id": continuity}

        status, trial = self._post("/api/shots/%d/preview" % sid, body)
        self.assertEqual(200, status)
        self.assertFalse(trial["saved"])
        self.assertEqual(shot["version"], trial["revision"])

        status, ok = self._post("/api/shots/%d/states" % sid,
                                {**body, "expected_revision": trial["revision"]})
        self.assertEqual(200, status)
        self.assertTrue(ok["saved"])

        status, stale = self._post("/api/shots/%d/states" % sid,
                                   {**body, "expected_revision": trial["revision"]})
        self.assertEqual(409, status)
        self.assertEqual(trial["revision"], stale["expected_revision"])
        self.assertIn("current_revision", stale)
        self.assertIn("server_state", stale)

        info = self._get("/api/shots/%d" % sid)
        self.assertEqual(info["shot"]["version"], stale["current_revision"])
        self.assertTrue(info["records"])


if __name__ == "__main__":
    unittest.main()
