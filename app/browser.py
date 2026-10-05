"""크롬으로 시작 페이지(네이버) 열기"""
import os
import subprocess
import webbrowser

from app.config import START_URL


def find_chrome():
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            path = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(path):
                return path
    return None


def open_start_page(url=START_URL):
    """크롬이 있으면 크롬으로, 없으면 기본 브라우저로 엶"""
    chrome = find_chrome()
    if chrome:
        try:
            subprocess.Popen([chrome, url])
            return
        except OSError:
            pass
    webbrowser.open(url)
