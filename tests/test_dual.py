import json
from pathlib import Path

from station.closing.seal import close
from station.closing.tally import CategoryCounts
from station.device.dual import observe_dual
from station.device.fixture import (FailOnceBeforeCommit, InjectedCommitFailure, OpticalFixture,
                                    OpticalObservation)
from station.device.recording import DeviceRecording
from station.device.trigger import trigger
from station.intake.accept import accept
from station.intake.dual import accept_dual
from station.intake.grading import Grade
from station.intake.repository import IntakeRepository
from station.lookup.round import get_round
from station.lookup.sample import get_sample
from station.rounds.authorization import authorize_read
from station.rounds.opening import open
from tests.support import StationCase

ROOT = Path(__file__).resolve().parent.parent


def _groups(messages):
    grouped = {'raw': [], 'evidence': []}
    for message in messages:
        grouped[message['kind']].append(message)
    return grouped


class DualCase(StationCase):
    def dual_piece(self, observations, round_id='dual', piece='S', limit=48, mode='dual'):
        open(self.connection, round_id, (piece,), limit, mode=mode)
        order_id = self.order_id(piece, round_id)
        authorize_read(self.connection, round_id, piece, order_id)
        exposure = trigger(self.connection, round_id, piece, order_id)
        produced = []
        for observation in observations:
            produced.append(_groups(observe_dual(self.connection, exposure, observation)))
        return exposure, produced, order_id


class DualFixtureTests(DualCase):
    def _run_case(self, case, fail_once=False):
        open(self.connection, 'dual', ('S',), 48, mode='dual')
        order_id = 'dual/S'
        authorize_read(self.connection, 'dual', 'S', order_id)
        exposure = trigger(self.connection, 'dual', 'S', order_id)
        observations = tuple(OpticalObservation(**row) for row in case['observations'])
        OpticalFixture(observations)
        produced = [_groups(observe_dual(self.connection, exposure, observation))
                    for observation in observations]
        conclusion = None
        for step in case['steps']:
            messages = []
            for index, channel in step['deliver']:
                messages.extend(produced[index][channel])
            if fail_once and step.get('fail_once'):
                fault = FailOnceBeforeCommit()
                with self.assertRaises(InjectedCommitFailure):
                    accept_dual(self.connection, order_id, messages[0], before_commit=fault)
                self.assertIsNone(IntakeRepository(self.connection).find_for_piece('dual', 'S'))
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM sample_receipts')
                                 .fetchone(), (0,))
            for message in messages:
                result = accept_dual(self.connection, order_id, message)
                if result is not None:
                    conclusion = result
            if step['expect'] is None:
                self.assertIsNone(conclusion, case['id'])
            else:
                self.assertIsNotNone(conclusion, case['id'])
                expected = step['expect']
                self.assertEqual(conclusion.conclusion.grade.value, expected['grade'], case['id'])
                if expected['grade'] == 'missing':
                    self.assertIsNone(conclusion.receipt, case['id'])
                else:
                    sample = get_sample(self.connection, conclusion.receipt.number)
                    self.assertEqual((sample.head, sample.tick, sample.raw_code),
                                     (expected['head'], expected['tick'], expected['raw_code']),
                                     case['id'])
        return conclusion

    def test_example_cases(self):
        data = json.loads((ROOT / 'examples' / 'first_read.json').read_text(encoding='utf-8'))
        for case in data['cases']:
            with self.subTest(case=case['id']):
                self.setUp()
                fail_once = 'late-earlier' == case['id']
                self._run_case(case, fail_once=fail_once)

    def test_missing_has_no_receipt(self):
        case = next(c for c in json.loads(
            (ROOT / 'examples' / 'first_read.json').read_text(encoding='utf-8'))['cases']
            if c['id'] == 'both-empty')
        conclusion = self._run_case(case)
        self.assertEqual(conclusion.conclusion.grade, Grade.MISSING)
        result = close(self.connection, 'dual').report
        self.assertEqual(result.counts, CategoryCounts(0, 0, 1))


class DualDeviceTests(DualCase):
    def test_mode_frozen(self):
        self.open_round(('A',))
        with self.assertRaises(ValueError):
            open(self.connection, 'day', ('A',), 48, mode='dual')
        self.assertEqual(open(self.connection, 'day', ('A',), 48, mode='single').mode, 'single')

    def test_single_round_rejects_dual_observe(self):
        self.open_round(('A',))
        exposure = self.start('A')
        with self.assertRaises(ValueError):
            observe_dual(self.connection, exposure, OpticalObservation('left', 10, 'reading', 48))

    def test_dual_round_rejects_single_entry(self):
        open(self.connection, 'dual', ('A',), 48, mode='dual')
        authorize_read(self.connection, 'dual', 'A', 'dual/A')
        exposure = trigger(self.connection, 'dual', 'A', 'dual/A')
        with self.assertRaises(ValueError):
            accept(self.connection, 'dual/A', DeviceRecording(self.connection).get_terminal(
                exposure.trigger_id) or object())

    def test_observation_order_and_conflicts(self):
        exposure, _, order_id = self.dual_piece([OpticalObservation('left', 10, 'reading', 48)])
        with self.assertRaises(ValueError):
            observe_dual(self.connection, exposure,
                         OpticalObservation('left', 9, 'empty'))
        with self.assertRaises(ValueError):
            observe_dual(self.connection, exposure,
                         OpticalObservation('left', 10, 'empty'))
        # Same observation replay produces no new physical action.
        replay = observe_dual(self.connection, exposure,
                              OpticalObservation('left', 10, 'reading', 48))
        self.assertEqual(_groups(replay)['evidence'][0]['reading_ticks'], [10])
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM dual_observations').fetchone(), (1,))

    def test_conflicting_observation_content(self):
        exposure, _, _ = self.dual_piece([OpticalObservation('left', 10, 'reading', 48)])
        with self.assertRaises(ValueError):
            observe_dual(self.connection, exposure, OpticalObservation('left', 10, 'reading', 49))

    def test_after_end_rejected(self):
        exposure, _, _ = self.dual_piece([OpticalObservation('left', 10, 'end')])
        with self.assertRaises(ValueError):
            observe_dual(self.connection, exposure, OpticalObservation('left', 11, 'reading', 48))


class DualIntakeTests(DualCase):
    def test_right_eleven_waits_for_left_ten(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 11, 'reading', 61),
            OpticalObservation('left', 10, 'reading', 48)])
        self.assertIsNone(accept_dual(self.connection, order_id, produced[0]['raw'][0]))
        self.assertIsNone(accept_dual(self.connection, order_id, produced[0]['evidence'][0]))
        self.assertIsNone(accept_dual(self.connection, order_id, produced[1]['evidence'][0]))
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('dual', 'S'))
        result = accept_dual(self.connection, order_id, produced[1]['raw'][0])
        self.assertEqual(result.conclusion.grade, Grade.PASS)
        result = accept_dual(self.connection, order_id, produced[0]['raw'][0])
        self.assertEqual(result.conclusion.grade, Grade.PASS)
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'left')
        accept_dual(self.connection, order_id, produced[1]['raw'][0])
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'left')

    def test_tie_prefers_left(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 10, 'reading', 61),
            OpticalObservation('left', 10, 'reading', 48)])
        accept_dual(self.connection, order_id, produced[0]['raw'][0])
        accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        self.assertIsNone(accept_dual(self.connection, order_id, produced[1]['evidence'][0]))
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('dual', 'S'))
        result = accept_dual(self.connection, order_id, produced[1]['raw'][0])
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'left')

    def test_right_tie_with_left_empty(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 10, 'reading', 61),
            OpticalObservation('left', 10, 'empty')])
        accept_dual(self.connection, order_id, produced[0]['raw'][0])
        accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        result = accept_dual(self.connection, order_id, produced[1]['evidence'][0])
        self.assertIsNotNone(result)
        self.assertEqual((result.conclusion.grade, get_sample(
            self.connection, result.receipt.number).head), (Grade.OVER_LIMIT, 'right'))

    def test_both_ended_missing(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('left', 9, 'empty'),
            OpticalObservation('right', 9, 'empty'),
            OpticalObservation('left', 10, 'end'),
            OpticalObservation('right', 10, 'end')])
        for index in (0, 1, 2):
            self.assertIsNone(accept_dual(self.connection, order_id, produced[index]['evidence'][0]))
        result = accept_dual(self.connection, order_id, produced[3]['evidence'][0])
        self.assertEqual(result.conclusion.grade, Grade.MISSING)
        self.assertIsNone(result.receipt)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM sample_receipts').fetchone(),
                         (0,))

    def test_signed_while_other_head_running(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 9, 'empty'),
            OpticalObservation('left', 10, 'reading', 48),
            OpticalObservation('right', 12, 'reading', 49)])
        self.assertIsNone(accept_dual(self.connection, order_id, produced[1]['raw'][0]))
        self.assertIsNone(accept_dual(self.connection, order_id, produced[1]['evidence'][0]))
        # Right has already advanced past tick 9, so left 10 is the first read.
        result = accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        self.assertIsNotNone(result)
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'left')
        # Later right observations do not disturb the fixed conclusion.
        accept_dual(self.connection, order_id, produced[2]['evidence'][0])
        accept_dual(self.connection, order_id, produced[2]['raw'][0])
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'left')

    def test_late_loser_cannot_rewrite(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('left', 10, 'reading', 48),
            OpticalObservation('right', 9, 'reading', 61)])
        # right 9 needs left evidence at tick 9; deliver left evidence (through 10) first.
        accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        accept_dual(self.connection, order_id, produced[1]['evidence'][0])
        accept_dual(self.connection, order_id, produced[1]['raw'][0])
        accept_dual(self.connection, order_id, produced[0]['raw'][0])
        sample = get_sample(self.connection, accept_dual(
            self.connection, order_id, produced[0]['raw'][0]).receipt.number)
        self.assertEqual((sample.head, sample.tick, sample.raw_code), ('right', 9, 61))

    def test_inflight_raw_is_not_empty(self):
        """A right read cannot sign while the left head may still have an earlier package in flight."""
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 9, 'reading', 61),
            OpticalObservation('left', 10, 'reading', 48)])
        accept_dual(self.connection, order_id, produced[0]['raw'][0])
        self.assertIsNone(accept_dual(self.connection, order_id, produced[0]['evidence'][0]))
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('dual', 'S'))
        # Advancing/ending the right head alone still proves nothing about the left head,
        # which could hold an earlier package in flight.
        result = accept_dual(self.connection, order_id, produced[1]['evidence'][0])
        self.assertIsNotNone(result)
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'right')

    def test_right_waits_for_left_ending(self):
        """Right progress cannot be treated as left emptiness; only an explicit left end proves it."""
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('left', 9, 'empty'),
            OpticalObservation('right', 10, 'reading', 61),
            OpticalObservation('left', 11, 'end')])
        for message in produced[1]['raw'] + produced[1]['evidence']:
            self.assertIsNone(accept_dual(self.connection, order_id, message))
        self.assertIsNone(accept_dual(self.connection, order_id, produced[0]['evidence'][0]))
        result = accept_dual(self.connection, order_id, produced[2]['evidence'][0])
        self.assertEqual((result.conclusion.grade,
                          get_sample(self.connection, result.receipt.number).head),
                         (Grade.OVER_LIMIT, 'right'))

    def test_unknown_order(self):
        with self.assertRaises(KeyError):
            accept_dual(self.connection, 'dual/X', {'trigger_id': 'x', 'kind': 'evidence',
                                                    'head': 'left', 'through': 1,
                                                    'event': 'end', 'reading_ticks': []})

    def test_conflicting_evidence_rejected(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 9, 'empty'),
            OpticalObservation('left', 10, 'reading', 48)])
        forged = dict(produced[1]['evidence'][0])
        forged['reading_ticks'] = [10, 11]
        with self.assertRaises(ValueError):
            accept_dual(self.connection, order_id, forged)
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('dual', 'S'))
        accept_dual(self.connection, order_id, produced[1]['raw'][0])
        self.assertIsNone(accept_dual(self.connection, order_id, produced[1]['evidence'][0]))
        result = accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        self.assertEqual(result.conclusion.grade, Grade.PASS)
        self.assertEqual(get_sample(self.connection, result.receipt.number).head, 'left')

    def test_fail_once_then_resume(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 9, 'empty'),
            OpticalObservation('left', 10, 'reading', 48)])
        accept_dual(self.connection, order_id, produced[1]['raw'][0])
        accept_dual(self.connection, order_id, produced[1]['evidence'][0])
        fault = FailOnceBeforeCommit()
        with self.assertRaises(InjectedCommitFailure):
            accept_dual(self.connection, order_id, produced[0]['evidence'][0], before_commit=fault)
        self.assertIsNone(IntakeRepository(self.connection).find_for_piece('dual', 'S'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM sample_receipts').fetchone(),
                         (0,))
        result = accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        self.assertIsNotNone(result)
        self.assertEqual(accept_dual(self.connection, order_id, produced[0]['evidence'][0]), result)


class DualCloseTests(DualCase):
    def test_pending_until_signed_then_close_and_offline(self):
        exposure, produced, order_id = self.dual_piece([
            OpticalObservation('right', 11, 'reading', 61),
            OpticalObservation('left', 10, 'reading', 48)])
        accept_dual(self.connection, order_id, produced[0]['raw'][0])
        self.assertEqual(close(self.connection, 'dual').pending, ('S',))
        self.assertIsNone(close(self.connection, 'dual').report)
        accept_dual(self.connection, order_id, produced[0]['evidence'][0])
        accept_dual(self.connection, order_id, produced[1]['evidence'][0])
        result = accept_dual(self.connection, order_id, produced[1]['raw'][0])
        report = close(self.connection, 'dual').report
        self.assertEqual(report.counts, CategoryCounts(1, 0, 0))
        self.connection.close()
        offline = self.offline()
        sample = get_sample(offline, result.receipt.number)
        self.assertEqual((sample.head, sample.tick, sample.raw_code), ('left', 10, 48))
        self.assertEqual(get_round(offline, report.number), report)
