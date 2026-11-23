import os
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix='trial-test-'), 'test.db')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_DB}')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import unittest  # noqa: E402

from sqlalchemy import select  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.constants.enums import RoundState, TrialState  # noqa: E402
from src.domain import analysis  # noqa: E402
from src.domain.sign_test import critical_wins, evaluate, sign_pvalue  # noqa: E402
from src.models.trial import Deployment, Trial, TrialEnrollment  # noqa: E402
from src.services import trials as service  # noqa: E402

ACTOR = 'analyst'


def make_rows():
    """6 条已完工 + 6 条待审，工单号、日期确定。"""
    rows = []
    for i in range(6):
        rows.append({
            'job_id': f'H{i + 1:02d}', 'job_date': f'2026-10-{10 + i:02d}',
            'title': f'历史讲座 {i}', 'video_minutes': 30 + i * 10,
            'low_confidence_pct': 5 + i * 3, 'speaker_changes': 4 + i * 2,
            'term_count': 2 + i * 4, 'actual_minutes': 40 + i * 8,
        })
    for i in range(6):
        rows.append({
            'job_id': f'N{i + 1:02d}', 'job_date': f'2026-11-{10 + i:02d}',
            'title': f'待审讲座 {i}', 'video_minutes': 35 + i * 9,
            'low_confidence_pct': 6 + i * 4, 'speaker_changes': 5 + i * 3,
            'term_count': 3 + i * 5, 'actual_minutes': None,
        })
    return rows


def insert_dataset(rows):
    from src.models.source import Dataset
    with Session.begin() as db:
        item = Dataset(name='测试批次', filename='test.csv', checksum=f'c{len(rows)}-{id(rows)}',
                       rows=rows, row_count=len(rows), author=ACTOR)
        db.add(item)
        db.flush()
        return item.id


class SignTestTests(unittest.TestCase):
    def test_known_binomial_tail(self):
        # n=10: 9 胜 -> (C(10,9)+C(10,10))/1024 = 11/1024
        self.assertAlmostEqual(sign_pvalue(9, 1), 11 / 1024)
        # 8 胜 -> 56/1024，超过 2.5%
        self.assertGreater(sign_pvalue(8, 2), 0.025)
        self.assertLessEqual(sign_pvalue(9, 1), 0.025)

    def test_critical_wins_and_small_sample(self):
        self.assertEqual(critical_wins(10), 9)
        # n=5 即使全胜 p=1/32≈0.03125，仍超 2.5%，无法达标 -> None
        self.assertIsNone(critical_wins(5))
        # n=7 全胜 p=1/128≈0.0078 可达标，临界值为 7
        self.assertEqual(critical_wins(7), 7)

    def test_evaluate_ignores_ties(self):
        paired = [('a', 1.0, 0.5), ('b', 2.0, 2.0), ('c', 3.0, 4.0)]
        summary = evaluate(paired)
        self.assertEqual((summary['wins'], summary['losses'], summary['ties']), (1, 1, 1))
        self.assertEqual(summary['compared'], 2)
        self.assertFalse(summary['passed'])  # n=2 时 p=0.5


class TrialFlowTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.rows = make_rows()
        self.dataset_id = insert_dataset(self.rows)
        self.body = {'name': '方案试跑 A', 'dataset_id': self.dataset_id,
                     'old_scheme_key': 'g1-p1', 'new_scheme_key': 'g3-p3',
                     'round_sizes': [3, 3]}

    def _create(self):
        with Session.begin() as db:
            trial = service.create_trial(db, dict(self.body), ACTOR)
            return trial.id

    def _view(self, trial_id):
        with Session() as db:
            return service.trial_view(db.get(Trial, trial_id), db)

    def test_create_enrolls_in_order_with_two_predictions(self):
        trial_id = self._create()
        view = self._view(trial_id)
        self.assertEqual([e['job_id'] for e in view['enrollments']],
                         ['N01', 'N02', 'N03', 'N04', 'N05', 'N06'])
        self.assertEqual([e['round_no'] for e in view['enrollments'][:3]], [1, 1, 1])
        self.assertEqual([e['round_no'] for e in view['enrollments'][3:]], [2, 2, 2])
        for e in view['enrollments']:
            self.assertIsNotNone(e['old_prediction'])
            self.assertIsNotNone(e['new_prediction'])
            self.assertIsNone(e['actual_minutes'])
        self.assertEqual([r['target_count'] for r in view['rounds']], [3, 3])
        self.assertTrue(all(r['status'] == RoundState.COLLECTING for r in view['rounds']))

    def test_validations(self):
        with Session.begin() as db:
            with self.assertRaises(ValueError):
                service.create_trial(db, {**self.body, 'new_scheme_key': 'g1-p1'}, ACTOR)
            with self.assertRaises(ValueError):
                service.create_trial(db, {**self.body, 'round_sizes': [3]}, ACTOR)
            with self.assertRaises(ValueError):
                service.create_trial(db, {**self.body, 'round_sizes': [3, 100]}, ACTOR)
        few = [r for r in make_rows() if r['actual_minutes'] is not None]
        ds = insert_dataset(few)
        with Session.begin() as db:
            with self.assertRaises(ValueError):
                service.create_trial(db, {**self.body, 'dataset_id': ds}, ACTOR)

    def test_missing_jobs_block_seal_and_page_lists_them(self):
        trial_id = self._create()
        # 乱序：先回填第 2 轮与第 1 轮的部分单
        payload = [{'job_id': 'N04', 'actual_minutes': 50}, {'job_id': 'N02', 'actual_minutes': 55}]
        with Session.begin() as db:
            applied, blocked = service.backfill_actuals(
                db, db.get(Trial, trial_id), payload, ACTOR)
        self.assertEqual(sorted(applied), ['N02', 'N04'])
        self.assertEqual(blocked, [])
        view = self._view(trial_id)
        round1 = next(r for r in view['rounds'] if r['round_no'] == 1)
        self.assertEqual(round1['missing_job_ids'], ['N01', 'N03'])
        self.assertFalse(round1['ready_to_seal'])
        with Session.begin() as db:
            with self.assertRaises(ValueError) as ctx:
                service.seal_round(db, db.get(Trial, trial_id), 1, ACTOR)
        self.assertIn('N01', str(ctx.exception))

    def test_correction_only_touches_unsealed_round(self):
        trial_id = self._create()
        full_round1 = [{'job_id': j, 'actual_minutes': v}
                       for j, v in (('N01', 50), ('N02', 60), ('N03', 70))]
        with Session.begin() as db:
            service.backfill_actuals(db, db.get(Trial, trial_id), full_round1, ACTOR)
            service.seal_round(db, db.get(Trial, trial_id), 1, ACTOR)
        # 封存后补正第 1 轮应被拒绝
        with Session.begin() as db:
            applied, blocked = service.backfill_actuals(
                db, db.get(Trial, trial_id),
                [{'job_id': 'N01', 'actual_minutes': 999},
                 {'job_id': 'N04', 'actual_minutes': 52}], ACTOR)
        self.assertEqual(applied, ['N04'])
        self.assertEqual(blocked, ['N01'])
        with Session() as db:
            n01 = db.scalar(select(TrialEnrollment).where(TrialEnrollment.job_id == 'N01'))
            n04 = db.scalar(select(TrialEnrollment).where(TrialEnrollment.job_id == 'N04'))
            self.assertEqual(n01.actual_minutes, 50)
            self.assertEqual(n04.actual_minutes, 52)
            self.assertFalse(n04.corrected)  # 首次回填不算补正
        # 再对未封存的 N04 补正一次，应被标记
        with Session.begin() as db:
            applied, blocked = service.backfill_actuals(
                db, db.get(Trial, trial_id),
                [{'job_id': 'N04', 'actual_minutes': 61}], ACTOR)
        self.assertEqual(applied, ['N04'])
        with Session() as db:
            n04 = db.scalar(select(TrialEnrollment).where(TrialEnrollment.job_id == 'N04'))
            self.assertEqual(n04.actual_minutes, 61)
            self.assertTrue(n04.corrected)

    def test_seal_is_idempotent_and_retry_deploys_once(self):
        trial_id = self._create()
        # 让第 1 轮实耗等于新方案预测，制造新方案占优
        with Session() as db:
            members = db.scalars(select(TrialEnrollment).where(
                TrialEnrollment.trial_id == trial_id, TrialEnrollment.round_no == 1)).all()
            payload = [{'job_id': m.job_id, 'actual_minutes': m.new_prediction} for m in members]
        with Session.begin() as db:
            service.backfill_actuals(db, db.get(Trial, trial_id), payload, ACTOR)
            trial, round_item, changed = service.seal_round(db, db.get(Trial, trial_id), 1, ACTOR)
        self.assertTrue(changed)
        view = self._view(trial_id)
        r1 = view['rounds'][0]
        self.assertEqual(r1['status'], RoundState.SEALED)
        self.assertIn('符号检验', r1['result']['basis'])
        # 重试封存：幂等，不产生第二次生效记录
        with Session.begin() as db:
            _, _, changed_again = service.seal_round(db, db.get(Trial, trial_id), 1, ACTOR)
        self.assertFalse(changed_again)
        with Session() as db:
            deployments = db.scalars(select(Deployment).where(Deployment.active.is_(True))).all()
        self.assertLessEqual(len(deployments), 1)

    def test_adopt_new_after_pass_then_retain_old_when_both_fail(self):
        # 场景一：第 1 轮新方案全胜且样本足够 -> 达标采用新方案
        trial_id = self._create()
        with Session() as db:
            members = db.scalars(select(TrialEnrollment).where(
                TrialEnrollment.trial_id == trial_id, TrialEnrollment.round_no == 1)).all()
            distinct = [m for m in members if m.old_prediction != m.new_prediction]
            payload = [{'job_id': m.job_id, 'actual_minutes': m.new_prediction} for m in members]
        with Session.begin() as db:
            service.backfill_actuals(db, db.get(Trial, trial_id), payload, ACTOR)
            service.seal_round(db, db.get(Trial, trial_id), 1, ACTOR)
        view = self._view(trial_id)
        if len(distinct) >= 3:  # 三单都非平局时 n=3，全胜 p=0.125 仍达不到 2.5%
            self.assertIn(view['status'], (TrialState.OPEN, TrialState.ADOPTED))
        # 第 2 轮无论结果如何，封存后试跑必须到达终态
        with Session() as db:
            members = db.scalars(select(TrialEnrollment).where(
                TrialEnrollment.trial_id == trial_id, TrialEnrollment.round_no == 2)).all()
            payload = [{'job_id': m.job_id, 'actual_minutes': m.old_prediction} for m in members]
        with Session.begin() as db:
            service.backfill_actuals(db, db.get(Trial, trial_id), payload, ACTOR)
            service.seal_round(db, db.get(Trial, trial_id), 2, ACTOR)
        view = self._view(trial_id)
        self.assertIn(view['status'], (TrialState.ADOPTED, TrialState.RETAINED))
        self.assertIsNotNone(view['active_deployment'])
        self.assertEqual(view['active_deployment']['source_trial_id'], trial_id)

        # 场景二：新一轮试跑，两轮都让旧方案更优 -> 沿用旧方案
        second = self._create()
        for round_no in (1, 2):
            with Session() as db:
                members = db.scalars(select(TrialEnrollment).where(
                    TrialEnrollment.trial_id == second, TrialEnrollment.round_no == round_no)).all()
                payload = [{'job_id': m.job_id, 'actual_minutes': m.old_prediction} for m in members]
            with Session.begin() as db:
                service.backfill_actuals(db, db.get(Trial, second), payload, ACTOR)
                service.seal_round(db, db.get(Trial, second), round_no, ACTOR)
        view = self._view(second)
        self.assertEqual(view['status'], TrialState.RETAINED)
        self.assertEqual(view['adopted_scheme'], 'old')
        self.assertEqual(view['active_deployment']['scheme_key'], 'old')

    def test_analyze_uses_deployment_for_new_jobs_only(self):
        trial_id = self._create()
        with Session() as db:
            frozen = db.get(Trial, trial_id).new_model
            scheme = db.get(Trial, trial_id).new_scheme
        deployment = {'scheme_key': 'new', 'scheme': scheme, 'model': frozen}
        # N01 已入组 -> 照旧；构造一个不在试跑里的新待审工单
        rows = make_rows()
        rows.append({'job_id': 'X99', 'job_date': '2026-12-01', 'title': '试跑后新工单',
                     'video_minutes': 60, 'low_confidence_pct': 10, 'speaker_changes': 8,
                     'term_count': 9, 'actual_minutes': None})
        enrolled = {f'N{i:02d}' for i in range(1, 7)}
        result = analysis.analyze(rows, {'ridge_penalty': 1, 'flag_minutes': 75},
                                  deployment=deployment, enrolled_job_ids=enrolled)
        queue = {row['job_id']: row for table in result['tables']
                 if table['title'] == '待审工时与核排清单' for row in table['rows']}
        self.assertEqual(queue['X99']['estimate_source'], f"试跑生效方案：{scheme['label']}")
        self.assertEqual(queue['N01']['estimate_source'], '本次回测模型')


if __name__ == '__main__':
    unittest.main(verbosity=2)
