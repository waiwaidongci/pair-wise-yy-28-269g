# 影视拍摄连续性管理

项目使用 Python 标准库、SQLite 和 `http.server` 管理非线性拍摄中的连续性。场次与镜头分别记录叙事顺序和拍摄顺序，角色、服装、道具、伤痕状态按叙事链检查，冲突可由调整方案或正式豁免处理，镜头只有在无未处理冲突时才能锁定。

## 运行与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8115`，页面 <http://127.0.0.1:8115>。首次启动创建“雨夜追踪”示例，其中拍摄顺序与叙事顺序相反，并生成一个伤痕回退冲突。数据库和端口可分别用 `CONTINUITY_DB`、`PORT` 指定。

## 连续性算法

每个元素选择一种规则：

- `stable`：沿叙事顺序状态必须一致。
- `monotonic`：使用 `numeric_value` 比较，数值不能下降，适合伤痕、污损或破坏程度。
- `allowed`：只有预先登记的状态转移才能通过。

检测按叙事顺序执行，与剪辑和拍摄顺序无关。调整方案必须由制片人或场记提出、由另一位审片人批准；批准后写入镜头状态并重新检查。也可以为确实需要保留的冲突写入豁免理由。锁定会再次检查场次，豁免之外的活跃冲突会阻止锁定，锁定后直接改状态会失败。

## 镜头状态：试算后确认

场记改状态不再一点提交就落库，而是分两步，避免填错后整组冲突被连带改写：

1. `POST /api/shots/{id}/preview`：按镜头**当前修订号**（`shots.version`）试算，纯读不保存。返回当前修订号、现在/拟改状态、将**新增/消除**的冲突清单、活跃冲突数量变化和**受影响镜头**（被编辑镜头与所有相关冲突两端镜头）。已锁定镜头和无权限用户在试算阶段即被拒绝。
2. `POST /api/shots/{id}/states`：确认时必须带回试算时的 `expected_revision`。服务端在事务内重新读取，若修订号已被别人推进则返回 **409**，并同时给出两边修订号、你提交的状态与服务器当前状态（`expected_revision`/`current_revision`/`submitted`/`server_state`），需要重新试算；一致才一次写入状态、递增修订号、同步冲突，并在 `state_change_records` 留下操作记录（旧值/新值、修订号变化、备注、操作人）。

`GET /api/shots/{id}` 返回镜头当前修订号、锁定状态、各元素状态和操作记录。审批通过的调整方案同样递增修订号并留记录。

## 主要接口

- `POST /api/users`、`POST /api/productions`
- `POST /api/productions/{id}/scenes`、`POST /api/scenes/{id}/shots`
- `POST /api/productions/{id}/elements`、`POST /api/elements/{id}/transitions`
- `POST /api/shots/{id}/preview`、`POST /api/shots/{id}/states`、`GET /api/shots/{id}`、`POST /api/scenes/{id}/check`
- `POST /api/conflicts/{id}/plans`、`POST /api/plans/{id}/review`
- `POST /api/conflicts/{id}/exemptions`
- `POST /api/shots/{id}/lock`
- `GET /api/productions/{id}/continuity`
