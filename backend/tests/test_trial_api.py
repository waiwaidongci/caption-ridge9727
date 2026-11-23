import os
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix='trial-api-'), 'api.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_DB}'
os.environ['JWT_SECRET'] = 'api-secret'
os.environ['SEED_DEMO'] = 'false'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6379/0'

import unittest  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402
from src.main import app  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

Base.metadata.create_all(engine)

with Session.begin() as db:
    for role, password in (('analyst', 'pw'), ('admin', 'pw'), ('viewer', 'pw')):
        if not db.get(User, role):
            db.add(User(username=role, role=role, password_hash=hash_password(password)))


def login(client, username):
    response = client.post('/auth/login', json={'username': username, 'password': 'pw'})
    assert response.status_code == 200, response.text
    return response.json()['token']


def auth(token):
    return {'Authorization': f'Bearer {token}'}


class TrialApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.analyst = login(cls.client, 'analyst')
        cls.viewer = login(cls.client, 'viewer')

    def test_full_trial_api_flow(self):
        client = self.client
        # 1. 可选方案
        schemes = client.get('/trials/schemes', headers=auth(self.analyst)).json()
        self.assertEqual(len(schemes['schemes']), 9)
        self.assertEqual(schemes['alpha'], 0.025)

        # 2. 导入演示批次（含 4 条待审），再追加待审单以满足两轮各 3 单
        rows = demo_rows()
        for i in range(4, 8):
            rows.append({'job_id': f'QJ-N{i + 1:02d}', 'job_date': '2026-11-12',
                         'title': f'补充待审 {i}', 'video_minutes': 40 + i, 'low_confidence_pct': 10,
                         'speaker_changes': 8, 'term_count': 12, 'actual_minutes': None})
        import io
        import csv
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        uploaded = client.post('/datasets', headers=auth(self.analyst),
                               json={'name': '接口试跑批次', 'filename': 'api.csv', 'csv_text': buffer.getvalue()})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        dataset_id = uploaded.json()['id']

        # 3. 校验：新旧方案不能相同
        bad = client.post('/trials', headers=auth(self.analyst),
                          json={'name': 't', 'dataset_id': dataset_id,
                                'old_scheme_key': 'g1-p1', 'new_scheme_key': 'g1-p1', 'round_sizes': [3, 3]})
        self.assertEqual(bad.status_code, 400)

        # 4. 创建试跑
        created = client.post('/trials', headers=auth(self.analyst),
                              json={'name': '接口试跑', 'dataset_id': dataset_id,
                                    'old_scheme_key': 'g1-p1', 'new_scheme_key': 'g3-p3',
                                    'round_sizes': [3, 3]})
        self.assertEqual(created.status_code, 201, created.text)
        trial = created.json()
        trial_id = trial['id']
        self.assertEqual(len(trial['enrollments']), 6)
        self.assertEqual(trial['enrollments'][0]['job_id'], 'QJ-N01')
        self.assertTrue(all(e['old_prediction'] is not None and e['new_prediction'] is not None
                            for e in trial['enrollments']))
        self.assertEqual(trial['rounds'][0]['missing_job_ids'], ['QJ-N01', 'QJ-N02', 'QJ-N03'])

        # 5. viewer 只读，不能创建/封存/回填
        self.assertEqual(client.post('/trials', headers=auth(self.viewer),
                                     json={'name': 'x', 'dataset_id': dataset_id,
                                           'old_scheme_key': 'g1-p1', 'new_scheme_key': 'g2-p1',
                                           'round_sizes': [2, 2]}).status_code, 403)

        # 6. 乱序回填：先给第 2 轮一单，再缺单封存应 409
        back = client.post(f'/trials/{trial_id}/backfill', headers=auth(self.analyst),
                           json={'rows': [{'job_id': 'QJ-N04', 'actual_minutes': 55}]})
        self.assertEqual(back.status_code, 200, back.text)
        self.assertEqual(back.json()['applied'], ['QJ-N04'])
        seal_fail = client.post(f'/trials/{trial_id}/seal/1', headers=auth(self.analyst))
        self.assertEqual(seal_fail.status_code, 409)
        self.assertIn('QJ-N01', seal_fail.json()['detail'])

        # 7. 收齐第 1 轮，实耗取新方案预测制造新方案占优
        detail = client.get(f'/trials/{trial_id}', headers=auth(self.analyst)).json()
        round1 = [e for e in detail['enrollments'] if e['round_no'] == 1]
        payload = [{'job_id': e['job_id'], 'actual_minutes': e['new_prediction']} for e in round1]
        client.post(f'/trials/{trial_id}/backfill', headers=auth(self.analyst), json={'rows': payload})
        sealed = client.post(f'/trials/{trial_id}/seal/1', headers=auth(self.analyst))
        self.assertEqual(sealed.status_code, 200, sealed.text)
        body = sealed.json()
        r1 = body['rounds'][0]
        self.assertEqual(r1['status'], 'sealed')
        self.assertIn('符号检验', r1['result']['basis'])
        # 封存后补正第 1 轮被忽略
        corrected = client.post(f'/trials/{trial_id}/backfill', headers=auth(self.analyst),
                                json={'rows': [{'job_id': round1[0]['job_id'], 'actual_minutes': 999},
                                               {'job_id': 'QJ-N05', 'actual_minutes': 60}]})
        self.assertEqual(corrected.json()['blocked_sealed'], [round1[0]['job_id']])
        self.assertEqual(corrected.json()['applied'], ['QJ-N05'])
        # 重试封存幂等
        again = client.post(f'/trials/{trial_id}/seal/1', headers=auth(self.analyst))
        self.assertEqual(again.status_code, 200)

        # 8. 收齐第 2 轮（取旧方案预测，旧方案占优），封存后到达终态并产生生效记录
        detail = client.get(f'/trials/{trial_id}', headers=auth(self.analyst)).json()
        round2 = [e for e in detail['enrollments'] if e['round_no'] == 2]
        payload = [{'job_id': e['job_id'], 'actual_minutes': e['old_prediction']} for e in round2]
        client.post(f'/trials/{trial_id}/backfill', headers=auth(self.analyst), json={'rows': payload})
        final = client.post(f'/trials/{trial_id}/seal/2', headers=auth(self.analyst)).json()
        self.assertIn(final['status'], ('adopted', 'retained'))
        self.assertIsNotNone(final['active_deployment'])
        self.assertEqual(final['active_deployment']['source_trial_id'], trial_id)

        # 9. 列表接口返回该试跑
        listing = client.get('/trials', headers=auth(self.viewer)).json()
        self.assertTrue(any(item['id'] == trial_id for item in listing))


if __name__ == '__main__':
    unittest.main(verbosity=2)
