import sqlite3
from dataclasses import dataclass

from station.device.fixture import OpticalObservation
from station.device.recording import DeviceRecording, ExposureRecord
from station.rounds.repository import RoundRepository, transaction

_HEADS = ('left', 'right')
_EVENTS = ('reading', 'empty', 'end')


def _encode(value: int) -> str:
    return hex(value)


def _decode(value: str) -> int:
    return int(value, 16)


def _require_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f'{field} must be an integer physical tick or raw code')
    return value


@dataclass(frozen=True)
class ParsedMessage:
    trigger_id: str
    kind: str
    head: str
    tick: int | None = None
    raw_code: int | None = None
    through: int | None = None
    event: str | None = None
    reading_ticks: tuple[int, ...] = ()


def parse_message(message: object) -> ParsedMessage:
    if not isinstance(message, dict):
        raise ValueError('delivered dual message must be a dictionary')
    trigger_id = message.get('trigger_id')
    if not isinstance(trigger_id, str) or not trigger_id.strip():
        raise ValueError('dual message requires a nonblank trigger id')
    kind = message.get('kind')
    head = message.get('head')
    if head not in _HEADS:
        raise ValueError('dual message requires a known head')
    if kind == 'raw':
        tick = _require_int(message.get('tick'), 'tick')
        raw_code = _require_int(message.get('raw_code'), 'raw_code')
        return ParsedMessage(trigger_id, 'raw', head, tick=tick, raw_code=raw_code)
    if kind == 'evidence':
        through = _require_int(message.get('through'), 'through')
        event = message.get('event')
        if event not in _EVENTS:
            raise ValueError('evidence requires a known event kind')
        listed = message.get('reading_ticks')
        if not isinstance(listed, (list, tuple)):
            raise ValueError('evidence requires a reading_ticks list')
        ticks = []
        for tick in listed:
            tick = _require_int(tick, 'reading tick')
            if tick > through:
                raise ValueError('evidence lists a reading beyond its observed tick')
            ticks.append(tick)
        if ticks != sorted(set(ticks)):
            raise ValueError('evidence reading ticks must be unique and ordered')
        return ParsedMessage(trigger_id, 'evidence', head, through=through, event=event,
                             reading_ticks=tuple(ticks))
    raise ValueError("dual message kind must be 'raw' or 'evidence'")


class DualObservationStore:
    """Fixed per-head device observations shared by one dual trigger."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS dual_observations (
                trigger_id TEXT NOT NULL REFERENCES exposures(trigger_id),
                head TEXT NOT NULL CHECK (head IN ('left', 'right')),
                position INTEGER NOT NULL CHECK (position >= 1),
                tick TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reading', 'empty', 'end')),
                raw_code TEXT,
                CHECK ((kind = 'reading' AND raw_code IS NOT NULL)
                    OR (kind != 'reading' AND raw_code IS NULL)),
                PRIMARY KEY (trigger_id, head, tick, kind),
                UNIQUE (trigger_id, head, position)
            )
        ''')

    def _rows(self, trigger_id: str, head: str):
        return self.connection.execute(
            '''SELECT position, tick, kind, raw_code FROM dual_observations
               WHERE trigger_id = ? AND head = ? ORDER BY position''',
            (trigger_id, head),
        ).fetchall()

    def find(self, trigger_id: str, head: str, tick: int, kind: str):
        return self.connection.execute(
            '''SELECT position, raw_code FROM dual_observations
               WHERE trigger_id = ? AND head = ? AND tick = ? AND kind = ?''',
            (trigger_id, head, _encode(tick), kind),
        ).fetchone()

    def readings_through(self, trigger_id: str, head: str, through: int) -> tuple[tuple[int, int], ...]:
        rows = self.connection.execute(
            '''SELECT tick, raw_code FROM dual_observations
               WHERE trigger_id = ? AND head = ? AND kind = 'reading' ORDER BY position''',
            (trigger_id, head),
        ).fetchall()
        return tuple((_decode(tick), _decode(code)) for tick, code in rows
                     if _decode(tick) <= through)

    def reading_ticks_through(self, trigger_id: str, head: str, through: int) -> tuple[int, ...]:
        return tuple(tick for tick, _ in self.readings_through(trigger_id, head, through))

    def record(self, exposure: ExposureRecord, observation: OpticalObservation) -> tuple[dict, ...]:
        if DeviceRecording(self.connection).find_exposure(exposure.order_id) != exposure:
            raise ValueError('observation targets an unknown or conflicting exposure')
        trigger_id = exposure.trigger_id
        existing = self.find(trigger_id, observation.head, observation.tick, observation.kind)
        if existing is not None:
            raw_code = None if observation.kind != 'reading' else _encode(observation.raw_code)
            if existing[1] != raw_code:
                raise ValueError('observation conflicts with the fixed device observation')
            return self._messages(trigger_id, observation.head, observation.tick, observation.kind,
                                  existing[0])
        rows = self._rows(trigger_id, observation.head)
        if rows:
            last_position, last_tick, last_kind, _ = rows[-1]
            if last_kind == 'end':
                raise ValueError(f'{observation.head} head has already ended')
            if observation.tick < _decode(last_tick):
                raise ValueError('observations must follow the physical order for each head')
            if observation.tick == _decode(last_tick) and observation.kind != 'end':
                raise ValueError('each head allows one reading or empty observation per tick')
            position = last_position + 1
        else:
            position = 1
        raw_code = _encode(observation.raw_code) if observation.kind == 'reading' else None
        self.connection.execute(
            '''INSERT INTO dual_observations
               (trigger_id, head, position, tick, kind, raw_code) VALUES (?, ?, ?, ?, ?, ?)''',
            (trigger_id, observation.head, position, _encode(observation.tick),
             observation.kind, raw_code),
        )
        return self._messages(trigger_id, observation.head, observation.tick, observation.kind,
                              position)

    def _messages(self, trigger_id: str, head: str, tick: int, kind: str,
                  position: int) -> tuple[dict, ...]:
        messages: list[dict] = []
        if kind == 'reading':
            row = self.connection.execute(
                '''SELECT raw_code FROM dual_observations
                   WHERE trigger_id = ? AND head = ? AND tick = ? AND kind = 'reading' ''',
                (trigger_id, head, _encode(tick)),
            ).fetchone()
            messages.append({'trigger_id': trigger_id, 'kind': 'raw', 'head': head,
                             'tick': tick, 'raw_code': _decode(row[0])})
        ticks = [t for t, _ in self._readings_before(trigger_id, head, position) if t <= tick]
        messages.append({'trigger_id': trigger_id, 'kind': 'evidence', 'head': head,
                         'through': tick, 'event': kind, 'reading_ticks': ticks})
        return tuple(messages)

    def _readings_before(self, trigger_id: str, head: str, position: int):
        rows = self.connection.execute(
            '''SELECT tick, raw_code FROM dual_observations
               WHERE trigger_id = ? AND head = ? AND kind = 'reading' AND position <= ?
               ORDER BY position''',
            (trigger_id, head, position),
        ).fetchall()
        return tuple((_decode(tick), _decode(code)) for tick, code in rows)


class DualRecorder:
    """Acquisition entry for a dual-mode round: validates and records observations."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        DualObservationStore(self.connection).initialize()

    def observe(self, exposure: ExposureRecord, observation: OpticalObservation) -> tuple[dict, ...]:
        store = DualObservationStore(self.connection)
        return store.record(exposure, observation)

    def verify(self, trigger_id: str, message: object) -> ParsedMessage:
        parsed = parse_message(message)
        if parsed.trigger_id != trigger_id:
            raise ValueError('delivered message has a conflicting trigger binding')
        store = DualObservationStore(self.connection)
        if parsed.kind == 'raw':
            row = store.find(trigger_id, parsed.head, parsed.tick, 'reading')
            if row is None or row[1] != _encode(parsed.raw_code):
                raise ValueError('delivered raw package differs from the device original')
        else:
            row = store.find(trigger_id, parsed.head, parsed.through, parsed.event)
            if row is None:
                raise ValueError('delivered evidence differs from the device original')
            expected = store.reading_ticks_through(trigger_id, parsed.head, parsed.through)
            if expected != parsed.reading_ticks:
                raise ValueError('delivered evidence conflicts with the device readings')
        return parsed


def observe_dual(connection: sqlite3.Connection, exposure: ExposureRecord,
                 observation: OpticalObservation) -> tuple[dict, ...]:
    with transaction(connection, write=True):
        if DeviceRecording(connection).find_exposure(exposure.order_id) != exposure:
            raise ValueError('observation targets an unknown or conflicting exposure')
        active = RoundRepository(connection).require_round(exposure.round_id)
        if not active.is_dual:
            raise ValueError('dual observations require a dual-mode round')
        return DualRecorder(connection).observe(exposure, observation)
