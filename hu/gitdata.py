# -*- coding: utf-8 -*-
"""حفظ قاعدة البيانات في فرع منفصل اسمه data (التزام واحد يُستبدل كل مرة — بلا تاريخ يكبر).

الفرع main يبقى للكود فقط. الموقع يُبنى من قاعدة البيانات الموجودة في فرع data.
"""
import os
import subprocess
from datetime import datetime, timezone
from typing import Dict, Optional


def _git(*args, check=True, cwd=None, inp: Optional[bytes] = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, input=inp, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.decode(errors='ignore')[:300]}")
    return r.stdout.decode(errors="ignore").strip()


def restore(repo: str, files: Dict[str, str]) -> bool:
    """يجلب آخر نسخة من فرع data. files: {اسم داخل الفرع: المسار المحلي}. يرجع True إذا وُجد الفرع."""
    r = subprocess.run(["git", "fetch", "--depth=1", "origin", "+refs/heads/data:refs/remotes/origin/data"],
                       cwd=repo, capture_output=True)
    if r.returncode != 0:
        return False
    for name, path in files.items():
        r = subprocess.run(["git", "show", f"origin/data:{name}"], cwd=repo, capture_output=True)
        if r.returncode == 0:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.stdout)
    return True


def publish(repo: str, files: Dict[str, str], message: str = "") -> str:
    """يكتب الملفات في التزام يتيم واحد ويدفعه بالقوة إلى فرع data."""
    lines = []
    for name, path in files.items():
        if os.path.exists(path):
            blob = _git("hash-object", "-w", path, cwd=repo)
            lines.append(f"100644 blob {blob}\t{name}")
    tree = _git("mktree", cwd=repo, inp=("\n".join(lines) + "\n").encode())
    msg = message or f"data {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
    env = dict(os.environ, GIT_AUTHOR_NAME="headsup-bot", GIT_AUTHOR_EMAIL="headsup-bot@users.noreply.github.com",
               GIT_COMMITTER_NAME="headsup-bot", GIT_COMMITTER_EMAIL="headsup-bot@users.noreply.github.com")
    r = subprocess.run(["git", "commit-tree", tree, "-m", msg], cwd=repo, capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("commit-tree: " + r.stderr.decode(errors="ignore")[:200])
    commit = r.stdout.decode().strip()
    last = None
    for _ in range(3):
        r = subprocess.run(["git", "push", "-f", "origin", f"{commit}:refs/heads/data"], cwd=repo, capture_output=True)
        if r.returncode == 0:
            return commit
        last = r.stderr.decode(errors="ignore")[:200]
    raise RuntimeError("push data: " + (last or ""))


def dispatch_site(repo: str) -> bool:
    """يطلب نشر الموقع (site.yml). GITHUB_TOKEN يستطيع تشغيل workflow_dispatch."""
    if not os.environ.get("GH_TOKEN"):
        return False
    r = subprocess.run(["gh", "workflow", "run", "site.yml", "--ref", "main"], cwd=repo, capture_output=True)
    return r.returncode == 0
