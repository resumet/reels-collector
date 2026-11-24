"""내부 스케줄러: 실행 종료 판정(5초)만 수행한다.

수집 자동 실행, 다운로드 재시도, 주기 알림 점검, 보관 기한 자동 정리는 없다.
알림 점검·보관 정리는 '보관·알림' 화면의 버튼으로 수동 실행한다.
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})


def start():
    from .services.collector import finalize_runs

    scheduler.add_job(finalize_runs, "interval", seconds=5, id="finalize")
    scheduler.start()
