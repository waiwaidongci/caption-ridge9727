from enum import StrEnum

class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'

class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'

class Review(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    RETURNED = 'returned'

class TrialState(StrEnum):
    OPEN = 'open'              # 试跑进行中，仍有未封存轮
    ADOPTED = 'adopted'        # 某一轮首次达标，采用新方案
    RETAINED = 'retained'      # 两轮均未达标，沿用旧方案

class RoundState(StrEnum):
    COLLECTING = 'collecting'  # 未封存：缺单等待、可补正
    SEALED = 'sealed'          # 已封存：判定冻结、补正不再影响
