"""실행 파일(exe)의 진입점."""
import os
import sys

# 창 모드 exe에는 콘솔이 없어서 표준 출력이 None이다. 진행 표시를 찍는 라이브러리가 죽지 않게 막아 둔다.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

if os.environ.get("CENSOR_DEBUG"):
    # 진단용: 일정 시간 뒤 모든 스레드가 어디에 있는지 파일로 남긴다.
    import faulthandler
    _dbg = open(os.path.join(os.environ.get("APPDATA", "."), "CensorStudio_debug.log"), "w", encoding="utf-8")
    faulthandler.enable(_dbg)
    faulthandler.dump_traceback_later(int(os.environ["CENSOR_DEBUG"]), repeat=False, file=_dbg)

from censor_studio.main import main

if __name__ == "__main__":
    main()
