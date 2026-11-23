"""方案试跑的状态逻辑：入组、实耗乱序回填、封存符号检验与采用决定。"""

from sqlalchemy import select
from src.constants.enums import RoundState, TrialState
from src.domain import analysis
from src.domain.sign_test import ALPHA, evaluate
from src.models.source import Dataset
from src.models.trial import Deployment, Trial, TrialEnrollment, TrialRound
from src.services.audit import record
from src.utils.time import now

MAX_TARGET = 500


def _rounds(trial_id, db):
    return list(db.scalars(select(TrialRound).where(TrialRound.trial_id == trial_id)
                           .order_by(TrialRound.round_no)))


def _enrollments(trial_id, db):
    return list(db.scalars(select(TrialEnrollment).where(TrialEnrollment.trial_id == trial_id)
                           .order_by(TrialEnrollment.position)))


def create_trial(db, body, actor):
    dataset = db.get(Dataset, body["dataset_id"])
    if not dataset:
        raise ValueError("找不到指定的数据批次")
    if body["old_scheme_key"] == body["new_scheme_key"]:
        raise ValueError("旧方案与新方案必须是两个不同的已完成方案")
    if len(body["round_sizes"]) != 2 or any(not isinstance(n, int) or n < 1 or n > MAX_TARGET
                                            for n in body["round_sizes"]):
        raise ValueError(f"两轮各看的单数需为 1 至 {MAX_TARGET} 之间的整数")
    ordered = sorted(analysis.validate_rows(dataset.rows),
                     key=lambda row: (row["job_date"], row["job_id"]))
    history = [row for row in ordered if row["actual_minutes"] is not None]
    if len(history) < 3:
        raise ValueError("试跑前至少需要三条已完工工单来冻结两个方案")
    pending = [row for row in ordered if row["actual_minutes"] is None]
    needed = sum(body["round_sizes"])
    if len(pending) < needed:
        raise ValueError(f"待审工单仅 {len(pending)} 条，不足以入组两轮共 {needed} 单")

    old_scheme = analysis.get_scheme(body["old_scheme_key"])
    new_scheme = analysis.get_scheme(body["new_scheme_key"])
    old_model = analysis.fit_scheme(history, old_scheme["key"])
    new_model = analysis.fit_scheme(history, new_scheme["key"])

    trial = Trial(name=body["name"], dataset_id=dataset.id, status=TrialState.OPEN, alpha=ALPHA,
                  old_scheme=old_scheme, new_scheme=new_scheme, old_model=old_model,
                  new_model=new_model, author=actor, updated_at=now())
    db.add(trial)
    db.flush()

    rounds = []
    for index, target in enumerate(body["round_sizes"], 1):
        round_item = TrialRound(trial_id=trial.id, round_no=index, target_count=target,
                                status=RoundState.COLLECTING)
        db.add(round_item)
        rounds.append(round_item)
    db.flush()

    # 按入组顺序（工单日期、工单号）取够两轮单数，每单留存旧、新两份预测。
    chosen = pending[:needed]
    cursor = 0
    for round_item, target in zip(rounds, body["round_sizes"]):
        for row in chosen[cursor:cursor + target]:
            cursor += 1
            db.add(TrialEnrollment(
                trial_id=trial.id, round_id=round_item.id, round_no=round_item.round_no,
                position=cursor, job_id=row["job_id"], job_date=row["job_date"], title=row["title"],
                old_prediction=analysis.predict_frozen(old_model, row),
                new_prediction=analysis.predict_frozen(new_model, row),
                actual_minutes=None))
    record(db, actor, "trial.created", trial.id, dataset_id=dataset.id,
           old=old_scheme["key"], new=new_scheme["key"], round_sizes=body["round_sizes"])
    return trial


def _round_view(round_item, enrollments):
    members = [item for item in enrollments if item.round_no == round_item.round_no]
    missing = [item.job_id for item in members if item.actual_minutes is None]
    ready = not missing
    payload = {
        "round_no": round_item.round_no,
        "target_count": round_item.target_count,
        "status": round_item.status,
        "enrolled": len(members),
        "filled": len(members) - len(missing),
        "missing_job_ids": missing,
        "ready_to_seal": ready and round_item.status == RoundState.COLLECTING,
        "sealed_at": round_item.sealed_at.isoformat() if round_item.sealed_at else None,
    }
    if round_item.result:
        payload["result"] = round_item.result
    return payload


def trial_view(trial, db):
    rounds = _rounds(trial.id, db)
    enrollments = _enrollments(trial.id, db)
    active = db.scalar(select(Deployment).where(Deployment.active.is_(True)).order_by(Deployment.id.desc()))
    return {
        "id": trial.id,
        "name": trial.name,
        "dataset_id": trial.dataset_id,
        "status": trial.status,
        "alpha": trial.alpha,
        "verdict": trial.verdict,
        "adopted_scheme": trial.adopted_scheme,
        "sealed_round_no": trial.sealed_round_no,
        "old_scheme": trial.old_scheme,
        "new_scheme": trial.new_scheme,
        "author": trial.author,
        "created_at": trial.created_at.isoformat() if trial.created_at else None,
        "updated_at": trial.updated_at.isoformat() if trial.updated_at else None,
        "rounds": [_round_view(item, enrollments) for item in rounds],
        "enrollments": [{
            "position": item.position,
            "round_no": item.round_no,
            "job_id": item.job_id,
            "job_date": item.job_date,
            "title": item.title,
            "old_prediction": item.old_prediction,
            "new_prediction": item.new_prediction,
            "actual_minutes": item.actual_minutes,
            "corrected": item.corrected,
            "sealed": next(r.status == RoundState.SEALED for r in rounds if r.round_no == item.round_no),
            "old_abs_error": round(abs(item.old_prediction - item.actual_minutes), 2)
            if item.actual_minutes is not None else None,
            "new_abs_error": round(abs(item.new_prediction - item.actual_minutes), 2)
            if item.actual_minutes is not None else None,
        } for item in enrollments],
        "active_deployment": None if not active else {
            "scheme_key": active.scheme_key,
            "label": active.scheme["label"],
            "source_trial_id": active.source_trial_id,
            "reason": active.reason,
            "author": active.author,
            "created_at": active.created_at.isoformat() if active.created_at else None,
        },
    }


def backfill_actuals(db, trial, rows, actor):
    """实耗乱序回填：按工单号匹配。封存轮的补正被拒绝，只影响未封存轮。

    rows 仅需 job_id 与 actual_minutes 两列，单元格已由路由按导入口径校验。
    """
    seen = set()
    by_job = {}
    for row in rows:
        job_id, value = row["job_id"], row["actual_minutes"]
        if not job_id or value is None:
            continue
        if job_id in seen:
            raise ValueError(f"回填数据中工单号 {job_id} 重复")
        seen.add(job_id)
        by_job[job_id] = value
    applied, blocked = [], []
    enrollments = _enrollments(trial.id, db)
    sealed_rounds = {item.round_no for item in _rounds(trial.id, db)
                     if item.status == RoundState.SEALED}
    for item in enrollments:
        value = by_job.get(item.job_id)
        if value is None:
            continue
        if item.round_no in sealed_rounds:
            if item.actual_minutes != value:
                blocked.append(item.job_id)
            continue
        if item.actual_minutes != value:
            if item.actual_minutes is not None:
                item.corrected = True  # 仅标记真正的补正，首次回填不算
            item.actual_minutes = value
            item.updated_at = now()
            applied.append(item.job_id)
    trial.updated_at = now()
    record(db, actor, "trial.backfilled", trial.id, applied=applied,
           blocked=blocked, note="封存轮补正已忽略" if blocked else None)
    return applied, blocked


def _deploy(db, trial, scheme_key, model, reason, actor):
    # 全局只保留一条生效记录；并发或重试下首次达标只生效一次。
    existing = db.scalar(select(Deployment).where(Deployment.active.is_(True)).with_for_update())
    if existing and existing.source_trial_id == trial.id:
        return existing
    if existing:
        existing.active = False
    deployment = Deployment(scheme_key=scheme_key, scheme=trial.new_scheme if scheme_key == "new"
                            else trial.old_scheme, model=model, source_trial_id=trial.id,
                            reason=reason, author=actor, active=True)
    db.add(deployment)
    return deployment


def seal_round(db, trial, round_no, actor):
    """封存一轮并做单侧精确符号检验。事务行锁保证并发验收/重试只生效一次。"""
    trial = db.get(Trial, trial.id, with_for_update=True)
    round_item = db.scalar(select(TrialRound).where(TrialRound.trial_id == trial.id,
                                                    TrialRound.round_no == round_no)
                           .with_for_update())
    if not round_item:
        raise ValueError("找不到该试跑轮次")
    if round_item.status == RoundState.SEALED:
        # 重试：封存是幂等的，直接返回既有判定，不重复采用、不重复审计。
        return trial, round_item, False
    members = [item for item in _enrollments(trial.id, db) if item.round_no == round_no]
    missing = [item.job_id for item in members if item.actual_minutes is None]
    if missing:
        raise ValueError("实耗未收齐，缺单：" + "、".join(missing))

    paired = [(item.job_id, abs(item.old_prediction - item.actual_minutes),
               abs(item.new_prediction - item.actual_minutes)) for item in members]
    summary = evaluate(paired)
    summary["target_count"] = round_item.target_count
    if summary["critical_wins"] is None:
        threshold_text = "本轮可比单数过少，即使新方案全部更优也无法达到门槛"
    else:
        threshold_text = f"至少 {summary['critical_wins']} 单新方案更优才达标"
    summary["basis"] = (
        f"第 {round_no} 轮 {summary['compared']} 单可比（平局 {summary['ties']} 单不计），"
        f"新方案更优 {summary['wins']} 单、旧方案更优 {summary['losses']} 单；"
        f"单侧精确符号检验 H0：新旧等概率，p={summary['p_value']:g}，"
        f"门槛 α={trial.alpha:g}（{threshold_text}）。"
    )
    round_item.status = RoundState.SEALED
    round_item.result = summary
    round_item.sealed_at = now()
    trial.sealed_round_no = round_no
    trial.updated_at = now()

    if summary["passed"] and trial.status == TrialState.OPEN:
        # 首次达标：采用新方案。已入组预测照旧，仅新工单改用新方案。
        trial.status = TrialState.ADOPTED
        trial.verdict = "adopted"
        trial.adopted_scheme = "new"
        reason = f"第 {round_no} 轮符号检验达标（p={summary['p_value']:g} ≤ {trial.alpha:g}），首次达标采用新方案"
        _deploy(db, trial, "new", trial.new_model, reason, actor)
        record(db, actor, "trial.adopted", trial.id, round_no=round_no, p_value=summary["p_value"])
    rounds = _rounds(trial.id, db)
    if trial.status == TrialState.OPEN and all(item.status == RoundState.SEALED for item in rounds):
        # 两轮都封存且都未达标：沿用旧方案。
        trial.status = TrialState.RETAINED
        trial.verdict = "retained"
        trial.adopted_scheme = "old"
        reason = "两轮符号检验均未达标，沿用旧方案"
        _deploy(db, trial, "old", trial.old_model, reason, actor)
        record(db, actor, "trial.retained", trial.id, reason=reason)
    record(db, actor, "trial.sealed", trial.id, round_no=round_no,
           passed=summary["passed"], p_value=summary["p_value"], wins=summary["wins"],
           losses=summary["losses"], ties=summary["ties"])
    return trial, round_item, True


def active_deployment(db):
    return db.scalar(select(Deployment).where(Deployment.active.is_(True)).order_by(Deployment.id.desc()))
