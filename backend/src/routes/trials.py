from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.analysis import SCHEMES
from src.models.identity import User
from src.models.trial import Trial
from src.schemas.requests import BackfillRequest, TrialRequest
from src.services import trials as trial_service
from src.services.validation import cell
from src.domain.spec import SPEC

router = APIRouter(prefix='/trials')


def require_trial(db, trial_id):
    item = db.get(Trial, trial_id)
    if not item:
        raise HTTPException(404, '找不到该试跑记录')
    return item


@router.get('/schemes')
def schemes(actor: User = Depends(current)):
    """试跑前可选的已完成方案口径（特征组 × 惩罚档）。"""
    return {'schemes': SCHEMES, 'alpha': 0.025}


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(Trial).order_by(Trial.id.desc()).limit(100))
    return [trial_service.trial_view(item, db) for item in items]


@router.post('', status_code=201)
def create(body: TrialRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        trial = trial_service.create_trial(db, body.model_dump(), actor.username)
        db.commit()
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return trial_service.trial_view(trial, db)


@router.get('/{trial_id}')
def detail(trial_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    return trial_service.trial_view(require_trial(db, trial_id), db)


@router.post('/{trial_id}/backfill')
def backfill(trial_id: int, body: BackfillRequest, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    trial = require_trial(db, trial_id)
    try:
        # 仅按工单号回填实耗，复用导入口径校验这两个单元格。
        fields = {field['key']: field for field in SPEC['fields']}
        rows = [{'job_id': cell(fields['job_id'], raw.get('job_id')),
                 'actual_minutes': cell(fields['actual_minutes'], raw.get('actual_minutes'))}
                for raw in body.rows]
        applied, blocked = trial_service.backfill_actuals(db, trial, rows, actor.username)
        db.commit()
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {'applied': applied, 'blocked_sealed': blocked,
            'trial': trial_service.trial_view(db.get(Trial, trial_id), db)}


@router.post('/{trial_id}/seal/{round_no}')
def seal(trial_id: int, round_no: int, db: Session = Depends(connection),
         actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    require_trial(db, trial_id)
    try:
        # 服务内部以行锁重读试跑与轮次，保证并发验收/重试只生效一次。
        trial, _, _ = trial_service.seal_round(db, db.get(Trial, trial_id), round_no, actor.username)
        db.commit()
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return trial_service.trial_view(db.get(Trial, trial_id), db)
