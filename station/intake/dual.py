import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from station.device.dual import DualRecorder, ParsedMessage, parse_message
from station.device.recording import DeviceRecording
from station.device.terminal import ConfirmedMissing, RawReading
from station.intake.accept import AcceptanceResult
from station.intake.grading import Grade, grade_terminal
from station.intake.receipt import ReceiptStore
from station.intake.repository import IntakeRepository
from station.rounds.repository import RoundRepository, transaction
from station.rounds.roster import require_name


def _encode(value: int) -> str:
    return hex(value)


def _decode(value: str) -> int:
    return int(value, 16)


@dataclass(frozen=True)
class HeadKnowledge:
    through: int | None
    reading_ticks: frozenset[int]
    ended: bool


class DualIntakeRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_raw (
                trigger_id TEXT NOT NULL REFERENCES exposures(trigger_id),
                head TEXT NOT NULL CHECK (head IN ('left', 'right')),
                tick TEXT NOT NULL,
                raw_code TEXT NOT NULL,
                PRIMARY KEY (trigger_id, head, tick)
            )
        ''')
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_evidence (
                trigger_id TEXT NOT NULL REFERENCES exposures(trigger_id),
                head TEXT NOT NULL CHECK (head IN ('left', 'right')),
                through TEXT NOT NULL,
                event TEXT NOT NULL CHECK (event IN ('reading', 'empty', 'end')),
                reading_ticks TEXT NOT NULL,
                PRIMARY KEY (trigger_id, head, through, event)
            )
        ''')
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_conclusions (
                conclusion_id INTEGER PRIMARY KEY REFERENCES piece_conclusions(conclusion_id),
                head TEXT NOT NULL CHECK (head IN ('left', 'right'))
            )
        ''')

    def _ticks_json(self, ticks) -> str:
        return json.dumps([_encode(tick) for tick in ticks])

    def record_raw(self, parsed: ParsedMessage) -> None:
        row = self.connection.execute(
            'SELECT raw_code FROM dual_raw WHERE trigger_id = ? AND head = ? AND tick = ?',
            (parsed.trigger_id, parsed.head, _encode(parsed.tick)),
        ).fetchone()
        encoded = _encode(parsed.raw_code)
        if row is not None:
            if row[0] != encoded:
                raise ValueError('delivered raw package conflicts with an accepted original')
            return
        self.connection.execute(
            'INSERT INTO dual_raw (trigger_id, head, tick, raw_code) VALUES (?, ?, ?, ?)',
            (parsed.trigger_id, parsed.head, _encode(parsed.tick), encoded),
        )

    def record_evidence(self, parsed: ParsedMessage) -> None:
        selected = self.connection.execute('''
            SELECT reading_ticks FROM dual_evidence
            WHERE trigger_id = ? AND head = ? AND through = ? AND event = ?
        ''', (parsed.trigger_id, parsed.head, _encode(parsed.through), parsed.event)).fetchone()
        payload = self._ticks_json(parsed.reading_ticks)
        if selected is not None:
            if json.loads(selected[0]) != json.loads(payload):
                raise ValueError('delivered evidence conflicts with an accepted fixture')
            return
        self.connection.execute('''
            INSERT INTO dual_evidence (trigger_id, head, through, event, reading_ticks)
            VALUES (?, ?, ?, ?, ?)
        ''', (parsed.trigger_id, parsed.head, _encode(parsed.through), parsed.event, payload))

    def head_knowledge(self, trigger_id: str, head: str) -> HeadKnowledge:
        rows = self.connection.execute(
            'SELECT through, event, reading_ticks FROM dual_evidence '
            'WHERE trigger_id = ? AND head = ?',
            (trigger_id, head),
        ).fetchall()
        through = None
        ended = False
        ticks: set[int] = set()
        for tick, event, payload in rows:
            tick = _decode(tick)
            if through is None or tick > through:
                through = tick
            if event == 'end':
                ended = True
            ticks.update(_decode(item) for item in json.loads(payload))
        return HeadKnowledge(through, frozenset(ticks), ended)

    def known_ticks(self, trigger_id: str, head: str) -> frozenset[int]:
        known = set(self.head_knowledge(trigger_id, head).reading_ticks)
        for (tick,) in self.connection.execute(
            'SELECT tick FROM dual_raw WHERE trigger_id = ? AND head = ?', (trigger_id, head)
        ):
            known.add(_decode(tick))
        return frozenset(known)

    def raw_package(self, trigger_id: str, head: str, tick: int) -> int | None:
        row = self.connection.execute(
            'SELECT raw_code FROM dual_raw WHERE trigger_id = ? AND head = ? AND tick = ?',
            (trigger_id, head, _encode(tick)),
        ).fetchone()
        return None if row is None else _decode(row[0])

    def winner_head_for(self, conclusion_id: int) -> str | None:
        row = self.connection.execute(
            'SELECT head FROM dual_conclusions WHERE conclusion_id = ?', (conclusion_id,)
        ).fetchone()
        return None if row is None else row[0]

    def insert_winner(self, conclusion_id: int, head: str) -> None:
        self.connection.execute(
            'INSERT INTO dual_conclusions (conclusion_id, head) VALUES (?, ?)',
            (conclusion_id, head),
        )


def _first_reading(duals: DualIntakeRepository, trigger_id: str):
    """Return the earliest known reading as ``(head, tick)``, left head winning tick ties."""
    left = duals.known_ticks(trigger_id, 'left')
    right = duals.known_ticks(trigger_id, 'right')
    combined = left | right
    if not combined:
        return None
    tick = min(combined)
    return ('left', tick) if tick in left else ('right', tick)


def _observed_through(head: HeadKnowledge, tick: int) -> bool:
    # An ended head will produce no further reads, so it can never hold an
    # earlier package at or beyond its end tick.
    if head.ended:
        return True
    return head.through is not None and head.through >= tick


def _coverage_ready(head: str, tick: int, left: HeadKnowledge, right: HeadKnowledge) -> bool:
    """Check the evidence that excludes earlier, or same-tick priority, readings.

    A candidate at ``tick`` needs its own head observed through ``tick - 1`` plus a
    delivered raw package at ``tick`` (checked separately).  Its opponent must be
    observed through ``tick - 1``; a right-head candidate also needs the left head
    observed through ``tick`` so a same-tick left read cannot be hidden.
    """
    own = left if head == 'left' else right
    other = right if head == 'left' else left
    required_other = tick if head == 'right' else tick - 1
    return _observed_through(own, tick - 1) and _observed_through(other, required_other)


def _replay(existing, receipts: ReceiptStore) -> AcceptanceResult:
    receipt = receipts.find_for_conclusion(existing.conclusion_id)
    if existing.grade == Grade.MISSING:
        if receipt is not None:
            raise RuntimeError('accepted missing conclusion has an inconsistent receipt')
    elif receipt is None:
        raise RuntimeError('accepted dual reading has an incomplete receipt')
    return AcceptanceResult(existing, receipt)


def accept_dual(connection: sqlite3.Connection, order_id: str, message: object, *,
                before_commit: Callable[[], None] | None = None) -> AcceptanceResult | None:
    require_name(order_id, 'order_id')
    rounds = RoundRepository(connection)
    intake = IntakeRepository(connection)
    receipts = ReceiptStore(connection)
    with transaction(connection, write=True):
        order = rounds.find_order(order_id)
        if order is None:
            raise KeyError(f'unknown read order {order_id!r}')
        parsed = parse_message(message)
        active = rounds.require_round(order.round_id)
        if not active.is_dual:
            raise ValueError(f'round {order.round_id!r} is not a dual-head round')
        exposure = DeviceRecording(connection).find_exposure(order_id)
        if exposure is None:
            raise ValueError(f'read order {order_id!r} has not triggered')
        verified = DualRecorder(connection).verify(exposure.trigger_id, message)
        if verified.trigger_id != exposure.trigger_id:
            raise ValueError('delivered message has a conflicting trigger binding')
        existing = intake.find_for_piece(order.round_id, order.piece)
        if existing is not None:
            return _replay(existing, receipts)
        if active.phase != 'open':
            raise ValueError(f'round {order.round_id!r} is sealed')
        duals = DualIntakeRepository(connection)
        if verified.kind == 'raw':
            duals.record_raw(verified)
        else:
            duals.record_evidence(verified)
        trigger_id = exposure.trigger_id
        left = duals.head_knowledge(trigger_id, 'left')
        right = duals.head_knowledge(trigger_id, 'right')
        first = _first_reading(duals, trigger_id)
        grade = None
        if first is not None:
            head, tick = first
            if not _coverage_ready(head, tick, left, right):
                return None
            code = duals.raw_package(trigger_id, head, tick)
            if code is None:
                return None
            source_id = f'original:{trigger_id}:{head}'
            original = RawReading(source_id, order.round_id, order.piece, order.order_id,
                                  trigger_id, tick, code)
            grade = grade_terminal(original, active.policy)
        else:
            if not (left.ended and right.ended):
                return None
            source_id = f'missing:{trigger_id}'
            original = ConfirmedMissing(source_id, order.round_id, order.piece, order.order_id,
                                        trigger_id)
            grade = Grade.MISSING
        stored = DeviceRecording(connection).finalize(original)
        conclusion = intake.insert_conclusion(stored, grade)
        if grade != Grade.MISSING:
            duals.insert_winner(conclusion.conclusion_id, first[0])
            receipt = receipts.issue(conclusion)
        else:
            receipt = None
        if before_commit is not None:
            before_commit()
    return AcceptanceResult(conclusion, receipt)
