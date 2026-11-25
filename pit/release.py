# -*- coding: utf-8 -*-
"""نشر المخزن وتنزيله كملفات Release باسم pit-data (البيانات أكبر من أن تُحفظ في فرع git).

  python -m pit.release upload   --dir pit_data      (يحتاج GH_TOKEN و gh CLI — متاح على Actions)
  python -m pit.release download --dir pit_data

أسماء Release مسطحة: daily/2016.parquet ← daily__2016.parquet
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

TAG = "pit-data"


def flat(rel: str) -> str:
    return rel.replace(os.sep, "/").replace("/", "__")


def unflat(name: str) -> str:
    return name.replace("__", "/")


def _gh(*args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def upload(d: str, repo: str) -> None:
    if _gh("release", "view", TAG, "-R", repo, check=False).returncode != 0:
        _gh("release", "create", TAG, "-R", repo, "--title", "PIT data",
            "--notes", "مخزن البيانات نقطي الزمن (يُستبدل عند كل بناء). انظر manifest.json.")
    stage = tempfile.mkdtemp()
    files = []
    for p in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
        if os.path.isfile(p) and (p.endswith(".parquet") or p.endswith("manifest.json")):
            rel = os.path.relpath(p, d)
            if rel.startswith("intraday"):
                continue                                  # ذاكرة الشموع داخل اليوم لا تُنشر هنا
            q = os.path.join(stage, flat(rel))
            shutil.copy(p, q)
            files.append(q)
    _gh("release", "upload", TAG, "-R", repo, "--clobber", *files)
    names = {os.path.basename(f) for f in files}
    old = json.loads(_gh("release", "view", TAG, "-R", repo, "--json", "assets").stdout)["assets"]
    for a in old:                                         # احذف ملفات سنة/جزء لم تعد موجودة
        if a["name"] not in names:
            _gh("release", "delete-asset", TAG, a["name"], "-R", repo, "-y", check=False)
    print(f"رُفع {len(files)} ملف إلى Release {TAG}")


def download(d: str, repo: str) -> bool:
    stage = tempfile.mkdtemp()
    r = _gh("release", "download", TAG, "-R", repo, "-D", stage, "--clobber", check=False)
    if r.returncode != 0:
        print(f"لا يوجد Release {TAG} بعد: {r.stderr.strip()[:200]}")
        return False
    for name in os.listdir(stage):
        dst = os.path.join(d, unflat(name))
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(os.path.join(stage, name), dst)
    print(f"نُزّل المخزن إلى {d}")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["upload", "download"])
    ap.add_argument("--dir", default="pit_data")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    a = ap.parse_args(argv)
    if not a.repo:
        sys.exit("حدد --repo أو GITHUB_REPOSITORY")
    os.makedirs(a.dir, exist_ok=True)
    (upload if a.action == "upload" else download)(a.dir, a.repo)


if __name__ == "__main__":
    main()
