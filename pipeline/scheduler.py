# -*- coding: utf-8 -*-
"""定时器：每天 07:29 触发 main.py 跑当日管线（7:30 前完成数据层启动）。

简单 schedule 循环，不引第三方调度框架。用法：
  python scheduler.py                 # 每天 07:29 跑真实管线
  python scheduler.py -- --mock-llm   # '--' 后的参数透传给 main.py
"""
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("scheduler")

TRIGGER_HOUR, TRIGGER_MINUTE = 7, 29  # 每天 07:29 触发
CHECK_INTERVAL = 30                   # 每 30 秒检查一次

MAIN_PY = Path(__file__).resolve().parent / "main.py"


def next_trigger(now: datetime) -> datetime:
    target = now.replace(hour=TRIGGER_HOUR, minute=TRIGGER_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_pipeline(extra_args: list):
    today = datetime.now().date().isoformat()
    # 生产默认走实时抓取；sample 仅用于回归重放
    cmd = [sys.executable, str(MAIN_PY), "--date", today, "--source", "live"] + list(extra_args)
    log.info("触发管线：%s", " ".join(cmd))
    subprocess.run(cmd, check=False)


def main():
    extra_args = sys.argv[1:]
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("定时器启动，每天 %02d:%02d 触发", TRIGGER_HOUR, TRIGGER_MINUTE)
    while True:
        now = datetime.now()
        target = next_trigger(now)
        wait = (target - now).total_seconds()
        log.info("下次触发：%s（%.0f 秒后）", target.strftime("%Y-%m-%d %H:%M"), wait)
        while wait > CHECK_INTERVAL:
            time.sleep(CHECK_INTERVAL)
            wait = (next_trigger(datetime.now()) - datetime.now()).total_seconds()
        time.sleep(max(wait, 0))
        run_pipeline(extra_args)


if __name__ == "__main__":
    main()
