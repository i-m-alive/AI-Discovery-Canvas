"""
opportunities — the Post-Workshop "Future Opportunities Register".

One row per adjacent-scope opportunity surfaced in discovery, scoped by
workshop. Rows come from the 'opportunities' agent (batch add with
normalized-title dedup, mirroring how `requirements` accumulates — a
re-run only ADDS, so the facilitator's own status triage (accept /
flag for pruning / reject) is never wiped by regeneration) and from
manual edits via the register UI. `opp_id` (OPP-01, ...) follows the
requirements table's REQ-NN convention: server-assigned, stable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, utc_now_column


class Opportunity(Base):
    __tablename__ = 'opportunities'

    id:          Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workshop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    opp_id:      Mapped[str] = mapped_column(String(16), nullable=False)   # 'OPP-01'
    title:       Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default='', server_default='')
    horizon:     Mapped[str] = mapped_column(String(20), nullable=False, default='explore', server_default='explore')  # phase_1|phase_2|phase_3|explore
    size:        Mapped[str] = mapped_column(String(2), nullable=False, default='M', server_default='M')               # S|M|L
    priority:    Mapped[str] = mapped_column(String(10), nullable=False, default='med', server_default='med')          # high|med|low
    status:      Mapped[str] = mapped_column(String(30), nullable=False, default='open', server_default='open')        # open|flagged_for_pruning|accepted|rejected
    # ['REQ-01', ...] — provenance back to the requirements table.
    source_req_ids: Mapped[list] = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')
    sort:        Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at:  Mapped[datetime] = utc_now_column()

    __table_args__ = (
        UniqueConstraint('workshop_id', 'opp_id', name='uq_opportunities_workshop_oppid'),
        Index('ix_opportunities_workshop', 'workshop_id'),
    )
