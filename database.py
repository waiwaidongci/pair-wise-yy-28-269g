from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


class RevisionConflict(DomainError):
    """Confirm was based on an outdated shot revision; carries both sides."""

    def __init__(self, payload: dict) -> None:
        super().__init__(payload.get("error", "镜头已被他人修改，请重新试算"))
        self.payload = payload


ELEMENT_KINDS = {"character", "costume", "prop", "injury"}
RULES = {"stable", "monotonic", "allowed"}


class ContinuityDB:
    """Non-linear film continuity checker with reviewable corrections."""

    def __init__(self, path: str = "continuity.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('producer','continuity','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS productions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              scene_number TEXT NOT NULL,
              title TEXT NOT NULL,
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              UNIQUE(production_id,scene_number),
              UNIQUE(production_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS shots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              shot_code TEXT NOT NULL,
              shoot_order INTEGER NOT NULL CHECK(shoot_order > 0),
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','locked')),
              version INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(scene_id,shot_code),
              UNIQUE(scene_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS elements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('character','costume','prop','injury')),
              rule TEXT NOT NULL CHECK(rule IN ('stable','monotonic','allowed')),
              description TEXT NOT NULL DEFAULT '',
              UNIQUE(production_id,name)
            );
            CREATE TABLE IF NOT EXISTS element_transitions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_state TEXT NOT NULL,
              to_state TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              UNIQUE(element_id,from_state,to_state)
            );
            CREATE TABLE IF NOT EXISTS element_states (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              state_value TEXT NOT NULL,
              numeric_value REAL,
              note TEXT NOT NULL DEFAULT '',
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(shot_id,element_id)
            );
            CREATE TABLE IF NOT EXISTS conflicts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_shot_id INTEGER NOT NULL REFERENCES shots(id),
              to_shot_id INTEGER NOT NULL REFERENCES shots(id),
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','exempted','resolved')),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              fingerprint TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adjustment_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              shot_id INTEGER NOT NULL REFERENCES shots(id),
              element_id INTEGER NOT NULL REFERENCES elements(id),
              new_value TEXT NOT NULL,
              numeric_value REAL,
              reason TEXT NOT NULL,
              proposed_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
              reviewed_by INTEGER REFERENCES users(id),
              review_note TEXT NOT NULL DEFAULT '',
              proposed_at TEXT NOT NULL,
              reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS exemptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              reason TEXT NOT NULL,
              approved_by INTEGER NOT NULL REFERENCES users(id),
              approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_change_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_value TEXT,
              from_numeric REAL,
              to_value TEXT NOT NULL,
              to_numeric REAL,
              from_version INTEGER NOT NULL,
              to_version INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              changed_by INTEGER NOT NULL REFERENCES users(id),
              changed_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        producer = self.add_user("制片", "producer")
        continuity = self.add_user("场记", "continuity")
        reviewer = self.add_user("审片", "reviewer")
        production = self.create_production("雨夜追踪", "非线性拍摄出的连续性示例", producer)
        scene = self.add_scene(production, "S01", "巷口相遇", 1)
        s01 = self.add_shot(scene, "S01-01", 2, 1, "角色受伤后", continuity)
        s02 = self.add_shot(scene, "S01-02", 1, 2, "角色尚未受伤", continuity)
        injury = self.add_element(production, "主角左臂伤痕", "injury", "monotonic", "伤痕严重程度只能递增")
        self.set_element_state(s01, injury, "重度", 3, "", continuity)
        self.set_element_state(s02, injury, "轻度", 1, "", continuity)
        self.check_scene(scene)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"producer", "continuity", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_production(self, title: str, description: str, user_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["role"] != "producer" or not title.strip():
            raise DomainError("只有制片人可以创建项目")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO productions(title,description,created_by,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), user_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def _production_for_user(self, production_id: int, user_id: int) -> sqlite3.Row:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise DomainError("用户不存在")
        if user["role"] == "reviewer":
            raise DomainError("审片人员只能审核方案和豁免，不能直接编排")
        return user

    def add_scene(self, production_id: int, scene_number: str, title: str, narrative_order: int) -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not scene_number.strip() or not title.strip() or narrative_order <= 0:
            raise DomainError("场次参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO scenes(production_id,scene_number,title,narrative_order) VALUES(?,?,?,?)",
                    (production_id, scene_number.strip(), title.strip(), narrative_order),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_shot(self, scene_id: int, shot_code: str, shoot_order: int, narrative_order: int,
                 description: str, user_id: int) -> int:
        scene = self.conn.execute("SELECT production_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        user = self._production_for_user(scene["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权创建镜头")
        if not shot_code.strip() or shoot_order <= 0 or narrative_order <= 0:
            raise DomainError("镜头参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO shots(scene_id,shot_code,shoot_order,narrative_order,description,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (scene_id, shot_code.strip(), shoot_order, narrative_order, description.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次内镜头编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_element(self, production_id: int, name: str, kind: str, rule: str, description: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not name.strip() or kind not in ELEMENT_KINDS or rule not in RULES:
            raise DomainError("连续性元素参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO elements(production_id,name,kind,rule,description) VALUES(?,?,?,?,?)",
                    (production_id, name.strip(), kind, rule, description.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目内元素名称不能重复") from exc
        return int(cur.lastrowid)

    def add_transition(self, element_id: int, from_state: str, to_state: str, note: str = "") -> int:
        element = self.conn.execute("SELECT rule FROM elements WHERE id=?", (element_id,)).fetchone()
        if not element or element["rule"] != "allowed":
            raise DomainError("只有 allowed 规则元素需要配置状态转移")
        if not from_state.strip() or not to_state.strip() or from_state == to_state:
            raise DomainError("状态转移必须包含两个不同状态")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO element_transitions(element_id,from_state,to_state,note) VALUES(?,?,?,?)",
                    (element_id, from_state.strip(), to_state.strip(), note.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该状态转移已存在") from exc
        return int(cur.lastrowid)

    def _validate_state_change(self, shot: sqlite3.Row, element: sqlite3.Row,
                               state_value: str, numeric_value: float | None, user_id: int) -> sqlite3.Row:
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权修改连续性状态")
        if shot["status"] == "locked":
            raise DomainError("镜头已锁定，不能直接修改状态")
        if not state_value.strip():
            raise DomainError("状态值不能为空")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则必须提供 numeric_value")
        return user

    def shot_info(self, shot_id: int) -> dict:
        shot = self.conn.execute(
            "SELECT s.*,sc.id AS scene_id,sc.scene_number,sc.production_id "
            "FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)
        ).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        states = []
        for row in self.conn.execute(
            "SELECT es.*,e.name AS element_name,e.kind,e.rule FROM element_states es "
            "JOIN elements e ON e.id=es.element_id WHERE es.shot_id=? ORDER BY e.id", (shot_id,)
        ).fetchall():
            states.append(dict(row))
        records = [dict(r) for r in self.conn.execute(
            "SELECT r.*,e.name AS element_name,u.name AS changed_by_name FROM state_change_records r "
            "JOIN elements e ON e.id=r.element_id JOIN users u ON u.id=r.changed_by "
            "WHERE r.shot_id=? ORDER BY r.id DESC", (shot_id,)
        ).fetchall()]
        return {"shot": dict(shot), "states": states, "records": records}

    def _shot_lookup(self, scene_id: int) -> dict[int, sqlite3.Row]:
        return {row["id"]: row for row in self.conn.execute(
            "SELECT * FROM shots WHERE scene_id=?", (scene_id,)).fetchall()}

    def _enrich_issues(self, issues: list[dict], lookup: dict[int, sqlite3.Row]) -> list[dict]:
        enriched = []
        for issue in issues:
            enriched.append({**issue,
                             "from_shot_code": lookup[issue["from_shot_id"]]["shot_code"],
                             "to_shot_code": lookup[issue["to_shot_id"]]["shot_code"]})
        return enriched

    def preview_element_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                              note: str, user_id: int) -> dict:
        """Dry-run the change against the shot's current revision; nothing is saved."""
        shot = self.conn.execute(
            "SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)
        ).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
        if not shot or not element or shot["production_id"] != element["production_id"]:
            raise DomainError("镜头与元素不属于同一项目")
        self._validate_state_change(shot, element, state_value, numeric_value, user_id)

        current = self.conn.execute(
            "SELECT * FROM element_states WHERE shot_id=? AND element_id=?", (shot_id, element_id)
        ).fetchone()
        current_payload = ({"state_value": current["state_value"], "numeric_value": current["numeric_value"]}
                           if current else None)
        proposed = {"state_value": state_value.strip(), "numeric_value": numeric_value}
        no_change = current is not None and current["state_value"] == state_value.strip() and (
            (current["numeric_value"] is None and numeric_value is None)
            or (current["numeric_value"] is not None and numeric_value is not None
                and float(current["numeric_value"]) == float(numeric_value)))

        lookup = self._shot_lookup(shot["scene_id"])
        baseline = self._detect_conflicts(shot["scene_id"])
        trial = self._detect_conflicts(shot["scene_id"], overrides={(shot_id, element_id): proposed})
        before_fp = {i["fingerprint"]: i for i in baseline}
        after_fp = {i["fingerprint"]: i for i in trial}
        added = self._enrich_issues([i for fp, i in after_fp.items() if fp not in before_fp], lookup)
        removed = self._enrich_issues([i for fp, i in before_fp.items() if fp not in after_fp], lookup)
        active_before = len(before_fp)
        active_after = len(after_fp)

        affected: dict[int, dict] = {}

        def mark(sid: int, via: str):
            row = lookup.get(sid)
            if not row:
                return
            affected.setdefault(sid, {
                "shot_id": sid, "shot_code": row["shot_code"],
                "narrative_order": row["narrative_order"], "reasons": set()})["reasons"].add(via)

        mark(shot_id, "edited")
        for issue in added:
            mark(issue["from_shot_id"], "new_conflict")
            mark(issue["to_shot_id"], "new_conflict")
        for issue in removed:
            mark(issue["from_shot_id"], "resolved_conflict")
            mark(issue["to_shot_id"], "resolved_conflict")
        affected_shots = []
        for item in sorted(affected.values(), key=lambda x: x["narrative_order"]):
            item["reasons"] = sorted(item["reasons"])
            affected_shots.append(item)

        return {
            "saved": False,
            "shot_id": shot_id,
            "element_id": element_id,
            "element_name": element["name"],
            "revision": shot["version"],
            "locked": shot["status"] == "locked",
            "current": current_payload,
            "proposed": proposed,
            "note": note.strip(),
            "no_change": no_change,
            "conflicts_added": added,
            "conflicts_removed": removed,
            "active_conflicts_before": active_before,
            "active_conflicts_after": active_after,
            "affected_shots": affected_shots,
        }

    def commit_element_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                             note: str, user_id: int, expected_version: int) -> dict:
        shot = self.conn.execute(
            "SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)
        ).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
        if not shot or not element or shot["production_id"] != element["production_id"]:
            raise DomainError("镜头与元素不属于同一项目")
        self._validate_state_change(shot, element, state_value, numeric_value, user_id)
        with self.transaction():
            current_shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
            if current_shot["status"] == "locked":
                raise DomainError("镜头已锁定，不能直接修改状态")
            if current_shot["version"] != expected_version:
                current_state = self.conn.execute(
                    "SELECT state_value,numeric_value FROM element_states WHERE shot_id=? AND element_id=?",
                    (shot_id, element_id),
                ).fetchone()
                raise RevisionConflict({
                    "error": f"镜头修订号已从 {expected_version} 变为 {current_shot['version']}，请重新试算后确认",
                    "shot_id": shot_id,
                    "element_id": element_id,
                    "expected_revision": expected_version,
                    "current_revision": current_shot["version"],
                    "submitted": {"state_value": state_value.strip(), "numeric_value": numeric_value},
                    "server_state": (dict(current_state) if current_state else None),
                })
            record_id, new_version = self._apply_state(
                shot_id, element_id, state_value.strip(), numeric_value, note.strip(), user_id,
                current_shot["version"])
            self._sync_conflicts(shot["scene_id"])
        return {"saved": True, "shot_id": shot_id, "element_id": element_id,
                "revision": new_version, "record_id": record_id,
                "conflicts": self.list_conflicts(shot["scene_id"])}

    def _apply_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                     note: str, user_id: int, from_version: int) -> tuple[int, int]:
        previous = self.conn.execute(
            "SELECT state_value,numeric_value FROM element_states WHERE shot_id=? AND element_id=?",
            (shot_id, element_id),
        ).fetchone()
        now = datetime.now().isoformat()
        try:
            self.conn.execute(
                "INSERT INTO element_states(shot_id,element_id,state_value,numeric_value,note,updated_by,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (shot_id, element_id, state_value, numeric_value, note, user_id, now),
            )
        except sqlite3.IntegrityError:
            self.conn.execute(
                "UPDATE element_states SET state_value=?,numeric_value=?,note=?,updated_by=?,updated_at=? "
                "WHERE shot_id=? AND element_id=?",
                (state_value, numeric_value, note, user_id, now, shot_id, element_id),
            )
        new_version = from_version + 1
        cur = self.conn.execute(
            "INSERT INTO state_change_records(shot_id,element_id,from_value,from_numeric,to_value,to_numeric,"
            "from_version,to_version,note,changed_by,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (shot_id, element_id,
             previous["state_value"] if previous else None,
             previous["numeric_value"] if previous else None,
             state_value, numeric_value, from_version, new_version, note, user_id, now),
        )
        self.conn.execute("UPDATE shots SET version=?,updated_by=?,updated_at=? WHERE id=?",
                          (new_version, user_id, now, shot_id))
        return int(cur.lastrowid), new_version

    def set_element_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                          note: str, user_id: int) -> dict:
        """Compatibility wrapper: commit against whatever revision the shot currently has."""
        shot = self.conn.execute("SELECT version FROM shots WHERE id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        result = self.commit_element_state(shot_id, element_id, state_value, numeric_value, note, user_id,
                                           shot["version"])
        return {"shot_id": shot_id, "element_id": element_id, "conflicts": result["conflicts"]}

    def _detect_conflicts(self, scene_id: int,
                          overrides: dict[tuple[int, int], dict] | None = None) -> list[dict]:
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        shots = self.conn.execute(
            "SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene_id,)
        ).fetchall()
        elements = self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (scene["production_id"],)).fetchall()
        overrides = overrides or {}
        detected: list[dict] = []
        for element in elements:
            sequence = []
            for shot in shots:
                state = overrides.get((shot["id"], element["id"]))
                if state is None:
                    state = self.conn.execute(
                        "SELECT * FROM element_states WHERE shot_id=? AND element_id=?", (shot["id"], element["id"])
                    ).fetchone()
                if state:
                    sequence.append((shot, state))
            for (prev_shot, prev), (shot, current) in zip(sequence, sequence[1:]):
                kind = None
                detail = ""
                if element["rule"] == "stable":
                    if current["state_value"] != prev["state_value"]:
                        kind = "state_changed"
                        detail = f"{element['name']} 应为稳定状态，却从 {prev['state_value']} 变为 {current['state_value']}"
                elif element["rule"] == "monotonic":
                    if current["numeric_value"] is None or prev["numeric_value"] is None:
                        kind = "missing_numeric_value"
                        detail = f"{element['name']} 缺少可比较的数值"
                    elif current["numeric_value"] < prev["numeric_value"]:
                        kind = "regression"
                        detail = f"{element['name']} 在叙事顺序中从 {prev['numeric_value']} 回退到 {current['numeric_value']}"
                else:
                    allowed = self.conn.execute(
                        "SELECT 1 FROM element_transitions WHERE element_id=? AND from_state=? AND to_state=?",
                        (element["id"], prev["state_value"], current["state_value"]),
                    ).fetchone()
                    if not allowed:
                        kind = "transition_not_allowed"
                        detail = f"{element['name']} 不允许从 {prev['state_value']} 变为 {current['state_value']}"
                if kind:
                    fingerprint = f"{scene_id}:{element['id']}:{prev_shot['id']}:{shot['id']}:{kind}"
                    detected.append({
                        "scene_id": scene_id, "element_id": element["id"], "element_name": element["name"],
                        "from_shot_id": prev_shot["id"], "to_shot_id": shot["id"], "kind": kind,
                        "detail": detail, "fingerprint": fingerprint,
                    })
        return detected

    def _sync_conflicts(self, scene_id: int) -> None:
        detected = self._detect_conflicts(scene_id)
        active_fingerprints = {row["fingerprint"] for row in detected}
        for row in self.conn.execute("SELECT * FROM conflicts WHERE scene_id=? AND active=1", (scene_id,)).fetchall():
            if row["fingerprint"] not in active_fingerprints:
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), row["id"]),
                )
        for issue in detected:
            existing = self.conn.execute("SELECT * FROM conflicts WHERE fingerprint=?", (issue["fingerprint"],)).fetchone()
            if existing:
                status = "exempted" if existing["status"] == "exempted" else "open"
                self.conn.execute(
                    "UPDATE conflicts SET active=1,status=?,detail=?,updated_at=? WHERE id=?",
                    (status, issue["detail"], datetime.now().isoformat(), existing["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO conflicts(scene_id,element_id,from_shot_id,to_shot_id,kind,detail,status,active,fingerprint,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?, 'open',1,?,?,?)",
                    (issue["scene_id"], issue["element_id"], issue["from_shot_id"], issue["to_shot_id"], issue["kind"], issue["detail"], issue["fingerprint"], datetime.now().isoformat(), datetime.now().isoformat()),
                )

    def check_scene(self, scene_id: int) -> list[dict]:
        if not self.conn.execute("SELECT 1 FROM scenes WHERE id=?", (scene_id,)).fetchone():
            raise DomainError("场次不存在")
        with self.transaction():
            self._sync_conflicts(scene_id)
        return self.list_conflicts(scene_id)

    def list_conflicts(self, scene_id: int, include_resolved: bool = False) -> list[dict]:
        clause = "" if include_resolved else "AND c.active=1"
        return [dict(row) for row in self.conn.execute(
            "SELECT c.*,e.name AS element_name,fs.shot_code AS from_shot_code,ts.shot_code AS to_shot_code "
            "FROM conflicts c JOIN elements e ON e.id=c.element_id JOIN shots fs ON fs.id=c.from_shot_id JOIN shots ts ON ts.id=c.to_shot_id "
            f"WHERE c.scene_id=? {clause} ORDER BY c.id", (scene_id,)
        ).fetchall()]

    def propose_adjustment(self, conflict_id: int, new_value: str, numeric_value: float | None,
                           reason: str, user_id: int) -> int:
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"]:
            raise DomainError("冲突不存在或已解决")
        if conflict["status"] != "open":
            raise DomainError("已豁免冲突不能提交状态调整方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (conflict["to_shot_id"],)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (conflict["element_id"],)).fetchone()
        user = self._production_for_user(element["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权提出调整方案")
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        if not new_value.strip() or len(reason.strip()) < 3:
            raise DomainError("新状态和调整理由必须填写")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则调整必须提供 numeric_value")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjustment_plans(conflict_id,shot_id,element_id,new_value,numeric_value,reason,proposed_by,proposed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (conflict_id, shot["id"], element["id"], new_value.strip(), numeric_value, reason.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已有调整方案") from exc
        return int(cur.lastrowid)

    def review_adjustment(self, plan_id: int, approve: bool, reviewer_id: int, note: str = "") -> dict:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        if not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("只有审片人可以审核调整方案")
        plan = self.conn.execute("SELECT * FROM adjustment_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "pending":
            raise DomainError("调整方案不存在或已审核")
        if plan["proposed_by"] == reviewer_id:
            raise DomainError("提案人不能审核自己的方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (plan["shot_id"],)).fetchone()
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        with self.transaction():
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE adjustment_plans SET status=?,reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?",
                (status, reviewer_id, note.strip(), datetime.now().isoformat(), plan_id),
            )
            if approve:
                current_shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (plan["shot_id"],)).fetchone()
                if current_shot["status"] == "locked":
                    raise DomainError("目标镜头已锁定")
                self._apply_state(
                    plan["shot_id"], plan["element_id"], plan["new_value"], plan["numeric_value"],
                    f"调整方案 #{plan_id}", reviewer_id, current_shot["version"],
                )
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), plan["conflict_id"]),
                )
                self._sync_conflicts(shot["scene_id"])
        return {"plan_id": plan_id, "status": status, "conflicts": self.list_conflicts(shot["scene_id"])}

    def approve_exemption(self, conflict_id: int, reason: str, reviewer_id: int) -> int:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"] or not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("冲突或审片人无效")
        if len(reason.strip()) < 8:
            raise DomainError("豁免理由至少8个字符")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO exemptions(conflict_id,reason,approved_by,approved_at) VALUES(?,?,?,?)",
                    (conflict_id, reason.strip(), reviewer_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已经豁免") from exc
            self.conn.execute("UPDATE conflicts SET status='exempted',updated_at=? WHERE id=?", (datetime.now().isoformat(), conflict_id))
        return int(cur.lastrowid)

    def lock_shot(self, shot_id: int, user_id: int) -> None:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权锁定镜头")
        with self.transaction():
            self._sync_conflicts(shot["scene_id"])
            blocking = self.conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE scene_id=? AND active=1 AND status!='exempted'", (shot["scene_id"],)
            ).fetchone()[0]
            if blocking:
                raise DomainError(f"场次仍有 {blocking} 个未处理冲突，不能锁定")
            self.conn.execute("UPDATE shots SET status='locked',version=version+1,updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), shot_id))

    def continuity_report(self, production_id: int) -> dict:
        production = self.conn.execute("SELECT * FROM productions WHERE id=?", (production_id,)).fetchone()
        if not production:
            raise DomainError("项目不存在")
        scenes = []
        for scene in self.conn.execute("SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)).fetchall():
            shots = [dict(r) for r in self.conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene["id"],))]
            conflicts = self.list_conflicts(scene["id"], include_resolved=True)
            scenes.append({**dict(scene), "shots": shots, "conflicts": conflicts})
        return {
            "production": dict(production),
            "elements": [dict(r) for r in self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,))],
            "scenes": scenes,
            "open_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "open"),
            "exempted_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "exempted"),
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "productions": [dict(r) for r in self.conn.execute("SELECT * FROM productions ORDER BY id")],
            "scenes": [dict(r) for r in self.conn.execute("SELECT * FROM scenes ORDER BY production_id,narrative_order")],
            "shots": [dict(r) for r in self.conn.execute("SELECT * FROM shots ORDER BY scene_id,narrative_order")],
        }
