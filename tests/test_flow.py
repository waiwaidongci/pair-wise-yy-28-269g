import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import ContinuityDB, DomainError, StateConflictError

class ContinuityFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=ContinuityDB(self.path)
        self.producer=self.db.add_user("制片","producer"); self.continuity=self.db.add_user("场记","continuity"); self.reviewer=self.db.add_user("审片","reviewer")
        self.production=self.db.create_production("测试影片","非线性拍摄",self.producer)
        self.scene=self.db.add_scene(self.production,"S01","雨夜",1)
        self.s1=self.db.add_shot(self.scene,"S01-01",2,1,"受伤后",self.continuity)
        self.s2=self.db.add_shot(self.scene,"S01-02",1,2,"受伤前",self.continuity)
        self.injury=self.db.add_element(self.production,"手臂伤痕","injury","monotonic","只能加重")
        self.db.set_element_state(self.s1,self.injury,"重度",3,"",self.continuity)
        self.db.set_element_state(self.s2,self.injury,"轻度",1,"",self.continuity)
    def tearDown(self): self.db.close(); os.unlink(self.path)
    def test_conflict_plan_review_and_lock_flow(self):
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual("regression",conflicts[0]["kind"])
        plan=self.db.propose_adjustment(conflicts[0]["id"],"重度",3,"将伤势调整到叙事顺序上的中间状态",self.continuity)
        result=self.db.review_adjustment(plan,True,self.reviewer,"通过")
        self.assertEqual([],result["conflicts"])
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.set_element_state(self.s2,self.injury,"重度",3,"重做",self.continuity)
    def test_exemption_and_validation_failures(self):
        conflicts=self.db.check_scene(self.scene)
        with self.assertRaisesRegex(DomainError,"单调规则"):
            self.db.set_element_state(self.s1,self.injury,"未知",None,"",self.continuity)
        self.db.approve_exemption(conflicts[0]["id"],"闪回镜头中伤痕表现属于刻意叙事误差",self.reviewer)
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        with self.assertRaisesRegex(DomainError,"只有审片人"):
            self.db.review_adjustment(999,True,self.continuity,"无权审核")
    def test_preview_confirm_with_revision_guard(self):
        conflicts=self.db.check_scene(self.scene)
        self.assertEqual(1,len(conflicts))
        v0=self.db.shot_detail(self.s2)["shot"]["version"]
        preview=self.db.preview_element_state(self.s2,self.injury,"重度",3,"试算改重度",self.continuity)
        self.assertEqual(v0,preview["base_version"])
        self.assertEqual([],preview["added_conflicts"])
        self.assertEqual(1,len(preview["removed_conflicts"]))
        self.assertEqual({self.s1,self.s2},{s["shot_id"] for s in preview["affected_shots"]})
        self.assertEqual("轻度",self.db.shot_detail(self.s2)["states"][0]["state_value"])
        self.assertEqual(v0,self.db.shot_detail(self.s2)["shot"]["version"])
        self.assertEqual([],self.db.shot_detail(self.s2)["logs"])
        self.db.set_element_state(self.s2,self.injury,"中度",2,"他人抢先修改",self.producer)
        with self.assertRaises(StateConflictError) as ctx:
            self.db.confirm_element_state(self.s2,self.injury,"重度",3,"确认",self.continuity,preview["base_version"])
        both=ctx.exception.payload["conflict"]
        self.assertEqual("中度",both["current_state"]["state_value"])
        self.assertEqual("重度",both["your_state"]["state_value"])
        self.assertEqual("制片",both["last_modified_by"])
        v1=self.db.shot_detail(self.s2)["shot"]["version"]
        result=self.db.confirm_element_state(self.s2,self.injury,"重度",3,"重新试算后确认",self.continuity,v1)
        self.assertEqual(v1+1,result["version"])
        self.assertEqual([],result["conflicts"])
        logs=self.db.shot_detail(self.s2)["logs"]
        self.assertEqual(1,len(logs))
        self.assertEqual(("中度","重度",v1,v1+1),(logs[0]["old_state_value"],logs[0]["new_state_value"],logs[0]["base_version"],logs[0]["new_version"]))
    def test_preview_added_conflicts_and_locked_shot(self):
        self.db.check_scene(self.scene)
        self.db.set_element_state(self.s2,self.injury,"重度",3,"先修复",self.continuity)
        preview=self.db.preview_element_state(self.s2,self.injury,"轻度",1,"试算回退",self.continuity)
        self.assertEqual(1,len(preview["added_conflicts"]))
        self.assertEqual("regression",preview["added_conflicts"][0]["kind"])
        self.assertEqual([],preview["removed_conflicts"])
        self.assertEqual([],self.db.list_conflicts(self.scene))
        self.db.lock_shot(self.s1,self.continuity); self.db.lock_shot(self.s2,self.continuity)
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.preview_element_state(self.s2,self.injury,"轻度",1,"",self.continuity)
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.confirm_element_state(self.s2,self.injury,"轻度",1,"",self.continuity,0)

if __name__=="__main__": unittest.main()
