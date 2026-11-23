from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import RoundState, TrialState
from src.utils.time import now


class Trial(Base):
    """一次工时估算方案试跑：试跑前选定旧、新两个已完成方案并冻结模型。"""

    __tablename__ = 'trials'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    status: Mapped[str] = mapped_column(String(20), default=TrialState.OPEN, index=True)
    alpha: Mapped[float] = mapped_column(default=0.025)
    # 冻结的方案口径（特征组、λ）与拟合参数，保证试跑期间预测不可变。
    old_scheme: Mapped[dict] = mapped_column(JSON)
    new_scheme: Mapped[dict] = mapped_column(JSON)
    old_model: Mapped[dict] = mapped_column(JSON)
    new_model: Mapped[dict] = mapped_column(JSON)
    # 全局结论，仅在首次达标或两轮全部封存且未达标时写入一次。
    verdict: Mapped[str | None] = mapped_column(String(20))
    adopted_scheme: Mapped[str | None] = mapped_column(String(20))
    sealed_round_no: Mapped[int | None]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime | None]


class TrialRound(Base):
    """试跑轮次：每轮预定单数，封存时做单侧精确符号检验。"""

    __tablename__ = 'trial_rounds'
    __table_args__ = (UniqueConstraint('trial_id', 'round_no'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'), index=True)
    round_no: Mapped[int] = mapped_column()
    target_count: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default=RoundState.COLLECTING, index=True)
    # 封存时冻结的判定依据；未封存为 None。
    result: Mapped[dict | None] = mapped_column(JSON)
    sealed_at: Mapped[datetime | None]


class TrialEnrollment(Base):
    """入组工单：按入组顺序分配位次与轮次，留存旧、新两份预测。"""

    __tablename__ = 'trial_enrollments'
    __table_args__ = (UniqueConstraint('trial_id', 'job_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('trials.id'), index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey('trial_rounds.id'), index=True)
    round_no: Mapped[int] = mapped_column(index=True)
    position: Mapped[int] = mapped_column()
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    job_date: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(300))
    old_prediction: Mapped[float]
    new_prediction: Mapped[float]
    # 实耗乱序回填；封存后该轮工单的实耗与判定不再被补正改动。
    actual_minutes: Mapped[float | None]
    corrected: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime | None]


class Deployment(Base):
    """当前生效的工时估算方案，试跑达标后写入（全局只保留一条生效记录）。"""

    __tablename__ = 'deployments'

    id: Mapped[int] = mapped_column(primary_key=True)
    scheme_key: Mapped[str] = mapped_column(String(20))  # old / new
    scheme: Mapped[dict] = mapped_column(JSON)
    model: Mapped[dict] = mapped_column(JSON)
    source_trial_id: Mapped[int | None] = mapped_column(ForeignKey('trials.id'))
    reason: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(default=True, index=True)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
