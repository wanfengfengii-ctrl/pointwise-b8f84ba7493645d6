# 澄湾输送线光学复检站

白班操作员按复检工单为具名试片开轮。值班员用合格、超限和已结束缺样三类结果安排放行、隔离或补测。夜班停用光学源后，凭原回执复核单片的原码和物理拍，凭班账号核对整轮结果。

## 运行

需要 Python 3.11 和标准库。在项目根目录执行完整回归。

```sh
python3 -B check_project.py
```

控制台在物化时提供标准检查入口。也可以直接运行同一测试包。

```sh
python3 -B -m unittest discover -s tests -t . -v
python3 -B -m station.rounds.workstation demo examples/shift.json examples/retry.json examples/dual.json
```

`demo` 为每份脚本使用独立临时库，经过发令、实收和封轮后关闭原连接，再从同一数据库领取。它核对脚本中的手算期望并输出班账号和样本回执。未预期错误和样例差异返回非零退出码。

```sh
python3 -B -m station.rounds.workstation init shift.sqlite3
python3 -B -m station.rounds.workstation run shift.sqlite3 examples/shift.json
python3 -B -m station.rounds.workstation sample shift.sqlite3 sample:1
python3 -B -m station.rounds.workstation round shift.sqlite3 round:shift-01
```

末两条使用本次 `run` 输出的实际编号。上例编号适用于从空库运行第一份脚本。`init` 可反复执行，保留原班轮、原读单、成果和编号。`run` 执行带手算期望的新班轮样例。修改样例数据时给班轮和读单分配新号，并更新手算期望。原班轮由公开开轮和领取接口按原号复领。

## 班次流程

1. `station.rounds.opening.open(conn, round_id, roster, limit)` 冻结有序唯一的试片清册和整数上限。清册可为空。班轮号、试片名和读单号是非空白字符串，并按原样比较。同参返回原班轮及当前阶段，异参报错。
2. `station.rounds.authorization.authorize_read(conn, round_id, piece, order_id)` 为开放轮的清册成员绑定全站唯一的原读单号。同参复领，试片或读单被其他绑定使用时报错。
3. `station.device.trigger.trigger(conn, round_id, piece, order_id)` 校验完整授权，返回固定的单头曝光记录。仅开放轮可发令。`station.device.optical.apply_observation(conn, exposure, observation)` 消费左头物理观察，出样固定原码及原拍，空拍返回 `None`，无样直至显式结束才固定缺样。曝光有样终局形成后，输入适配器停止向该录制推进。
4. `station.intake.accept.accept(conn, order_id, terminal)` 核对设备持久终局和请求读单。有样原码以无换算的整数原码单位直接与上限比较，严格大于上限判 `over_limit`，其余判 `pass`。原码 0 有效。明确缺样判 `missing`，没有样本回执。提交成功后返回含 `conclusion` 和可选 `receipt` 的结果。同件复领首执。提交失败时，可用 `DeviceRecording(conn).get_terminal(trigger_id)` 取原件续办。
5. `station.closing.seal.close(conn, round_id)` 返回 `CloseResult`。在办时 `pending` 按清册排列且 `report` 为空。齐全时 `report` 含固定班账号、清册顺序的逐片结论和三个计数，另行调用沿用原账。设备已结束但尚未实收的试片仍在办。
6. `station.lookup.sample.get_sample(conn, receipt_number)` 返回原判级、原码、物理拍、原触发和读单。`station.lookup.round.get_round(conn, report_number)` 返回原班账和固定逐片结论。领取只需要数据库连接。

用 `station.rounds.workstation.initialize_station(conn)` 初始化可写连接。公开写入口负责各自提交，领取在同一读取事务内核对相关引用。调用公开入口时连接应无活动事务。输入冲突或不合法操作报 `ValueError`，未知号报 `KeyError`，损坏的领取引用报 `ReferenceIntegrityError`，SQLite 写入及提交错误向调用方报告。

## 双头首读

双头班轮沿用同一清册、授权和严格超限口径，两个读头共用同一触发号，头位为 `left` 或 `right`。

1. `station.rounds.opening.open(conn, round_id, roster, limit, *, mode="dual")` 以双头模式开轮，模式随开轮冻结，同号异参拒绝。
2. `station.device.trigger.trigger` 签名照旧，双头同参复投沿用原触发。
3. `station.device.optical.observe_dual(conn, exposure, observation)` 按头推进源序观察，返回本次产生的消息。每个 `reading` 首处理时产生可独立递送的原包（`kind="raw"`，含 `head`、`tick`、`raw_code`），每次观察另产生证据消息（`kind="evidence"`，含 `head`、`through`、`event` 和已读出的 `reading_ticks`；证据只标出有原读的拍以区分空拍与在途原包，不含原码）。`end` 可紧随同拍观察，已出样的原包仍可在途。错绑、倒序或同身份内容冲突拒绝，重投不增加物理动作。
4. `station.intake.accept.accept_dual(conn, order_id, message, *, before_commit=None)` 递交原包或证据。尚待证返回 `None`；具备原包与资格证据时直接签发 `AcceptanceResult`；两头已证结束且无有效原读时返回 `missing` 结论且 `receipt` 为空。首读按最早物理拍，同拍左先；左侧候选只须排除右侧更早原读，右侧候选还须排除左侧同拍原读。证据不能充作原包，仅头推进或结束不能把在途原包当空拍。错绑或内容冲突报 `ValueError`，未知读单报 `KeyError`。证据齐备即签，即使另一头仍在运行；迟到的非胜出件不能改写已签成果。
5. 签发一次提交结论、采用的原件引用和首执。`before_commit` 在新结论提交前调用；失败不留新结果或回执，已接纳事实保持可用，重投同一消息即可续办。
6. `station.lookup.sample.get_sample` 对双头样本另返回实际胜出的 `head`。待证成员仍在办，已签首读或已确认缺样才入班账。停源重开同一站库后仍按原回执领取。

单头入口（`apply_observation`、`accept`）及其既有成果照旧，且不得用于绕过双头资格。

## 双头手算样例

`examples/dual.json` 的清册是 L、M，上限 48，双头模式。L 右头第 11 拍读 61，左头第 10 拍读 48。送达次序先给右 11 的原包和证据，接着推进 M，再给左 10 的证据；此时左头第 10 拍原包迟到，仍待证。左 10 原包到达后按左头第 10 拍签收，迟到的右 11 不改写成果。M 两头各只空拍后结束，取证齐备即判缺样、不发回执。封轮后停源重开，按原回执领取 L 的 `head="left"`、第 10 拍、原码 48。

`examples/first_read.json` 的六个场景覆盖最早拍、同拍左先、同拍空拍、左先于右同拍、结束后迟到原包，以及两头结束无有效原读。`python3 -B check_first_read_fixture.py examples/first_read.json` 只核对原始观察和送达安排；回归 `tests/test_dual.py` 会按这些步骤递交消息并核对逐步期望，`late-earlier` 场景还验证失败后续办。

## 两份手算样例

- `shift.json` 的清册是 C、A、B，上限 48。A 第 10 拍读 49，B 只空拍后结束，C 第 12 拍读 0。按 A、B、C 送达，A 实收后在办是 C、B。最后仍按 C、A、B 封轮，合格、超限、缺样各一。
- `retry.json` 的上限也是 48。D 第 10 拍读 48，送达时点 31。E 第 11 拍读 49，先到。D 首次提交前发生一次可控失败，随后沿原终局续办。班账为一合格、一超限、零缺样，按原回执领取 D 仍为第 10 拍。

物理拍与送达时点是分别指定的整数。脚本按头保存原始的出样、空拍和结束观察，调度按送达时点及入队次序交付。原读码、物理拍、判级和清册位置可逐项复核。编号以本库首次成功提交所得到的编号为准。
