#!/usr/bin/env python3
"""공급망·코드 무결성 보안 도구 (단일 스크립트).

    python3 security_lock.py generate   # requirements.lock(해시) + security_manifest.json 생성/갱신
    python3 security_lock.py verify      # 무결성 검증(트립와이어). 변조 시 exit 1.

용도:
  - generate : 코드 변경/배포(git pull/커밋) 후 운영자가 1회 실행해 기준선을 갱신하고 커밋.
  - verify   : 봇 시작 시(main.py) 자동 호출 + 운영자/CI 수동. 디스크 파일이 커밋된 기준선과
               다르면 = "악의적 함수 변형/악성코드 삽입" 의심으로 경보(트립와이어).

방어 한계(정직): 기준선(security_manifest.json)·락은 git에 커밋해 보호한다. 쓰기 권한을
가진 공격자는 기준선까지 재생성할 수 있으나, 그러면 git diff에 드러난다(레포 측 탐지).
런타임 트립와이어는 "배포 후 디스크 무단 변경"을 잡는 방어선이다(공급망/인사이더 변조).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Optional

REPO = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(REPO, "security_manifest.json")
LOCKFILE = os.path.join(REPO, "requirements.lock")

# 무결성 추적 대상: 코드(.py) + 설정(config.yaml). 변경 잦은/생성물은 제외.
EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "data", "logs",
                ".claude", "node_modules", ".pytest_cache", ".mypy_cache"}
TRACK_SUFFIX = (".py",)
TRACK_NAMES = {"config.yaml"}
LOCK_PYTHON = (3, 8)  # 운영·Security CI의 최소 지원 버전


def _reexec_generate_with_lock_python() -> Optional[int]:
    """락은 운영 기준 Python 3.8 환경에서만 생성한다.

    개발 셸의 최신 Python(예: 3.13)로 설치된 전이 의존성을 그대로 고정하면
    Python 3.8에서 설치할 수 없는 버전까지 lock에 들어간다. 사용 가능한 3.8을
    자동 탐색해 전체 generate를 재실행하고, 없으면 잘못된 락을 만들지 않고 중단한다.
    """
    if sys.version_info[:2] == LOCK_PYTHON:
        return None

    candidates = [shutil.which("python3.8"), "/usr/bin/python3"]
    current = os.path.realpath(sys.executable)
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.realpath(candidate)
        if candidate in seen or candidate == current or not os.path.isfile(candidate):
            continue
        seen.add(candidate)
        try:
            version = subprocess.check_output(
                [candidate, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                text=True,
                timeout=10,
            ).strip()
        except Exception:
            continue
        if version == "%d.%d" % LOCK_PYTHON:
            print(f"의존성 락 호환성: {sys.version_info.major}.{sys.version_info.minor} 대신 "
                  f"{candidate} (Python {version})로 재실행")
            return subprocess.call([candidate, os.path.abspath(__file__), "generate"])

    print("오류: requirements.lock은 운영 호환성을 위해 Python 3.8로 생성해야 합니다. "
          "python3.8을 설치하거나 해당 인터프리터로 다시 실행하세요.", file=sys.stderr)
    return 2


def _iter_source_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(TRACK_SUFFIX) or f in TRACK_NAMES:
                yield os.path.relpath(os.path.join(root, f), REPO)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest() -> dict:
    out = {}
    for rel in _iter_source_files():
        p = os.path.join(REPO, rel)
        if os.path.exists(p):
            out[rel] = _sha256(p)
    return out


def verify_files() -> tuple:
    """기준선 대비 무결성 검증. 반환 (ok: bool, mismatches: list[(rel, why)]).

    기준선이 없으면 통과(미설정). main.py 에서 import 해 startup 트립와이어로 사용.
    """
    if not os.path.exists(MANIFEST):
        return True, []
    try:
        base = json.load(open(MANIFEST, encoding="utf-8")).get("files", {})
    except Exception as e:
        return False, [("security_manifest.json", f"기준선 로드 실패: {e}")]
    current = build_manifest()
    mm = []
    for rel, h in base.items():
        cur = current.get(rel)
        if cur is None:
            mm.append((rel, "삭제됨"))
        elif cur != h:
            mm.append((rel, "변경됨"))
    for rel in current:
        if rel not in base:
            mm.append((rel, "신규(기준선 외)"))
    return (len(mm) == 0), mm


def generate_manifest():
    manifest = build_manifest()
    payload = {"version": 1, "count": len(manifest), "files": manifest}
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
    print(f"무결성 매니페스트: {len(manifest)}개 파일 → {MANIFEST}")


def generate_lock():
    """설치 패키지(importlib.metadata) + PyPI sha256 으로 해시 핀 requirements.lock 생성.

    설치 검증: pip install --require-hashes -r requirements.lock
    pip/pip-tools 불필요(시스템 pip 깨짐 환경에서도 동작). 조회 실패는 주석 처리.
    """
    try:
        import importlib.metadata as im
    except Exception:
        print("⚠️ importlib.metadata 불가 — requirements.lock 생략")
        return
    try:
        import requests
    except Exception:
        print("⚠️ requests 없음 — 해시 조회 불가, requirements.lock 생략")
        return

    # requirements.txt 를 시드로 의존성 클로저만 수집 (OS 시스템 패키지 오염 방지)
    import re

    def _canon(n):
        return re.sub(r"[-_.]+", "-", (n or "").strip().lower())

    def _req_name(spec):
        m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec or "")
        return m.group(1) if m else ""

    seeds = []
    req_txt = os.path.join(REPO, "requirements.txt")
    if os.path.exists(req_txt):
        for line in open(req_txt, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            n = _req_name(line)
            if n:
                seeds.append(n)
    installed, stack, seen = {}, list(seeds), set()
    while stack:
        nm = stack.pop()
        key = _canon(nm)
        if key in seen:
            continue
        seen.add(key)
        try:
            try:
                dist = im.distribution(nm)
            except im.PackageNotFoundError:
                # importlib.metadata(Python 3.8)는 ``discord.py`` dist-info를
                # 점이 포함된 원명으로 못 찾고 canonical name(discord-py)만 인식한다.
                dist = im.distribution(key)
            real = (dist.metadata["Name"] or nm).strip()
            ver = (dist.version or "").strip()
        except Exception:
            continue
        if ver and real.lower() not in ("pip", "setuptools", "wheel"):
            installed[real] = ver
        for req in (dist.requires or []):
            if "; extra ==" in req or "extra==" in req.replace(" ", ""):
                continue  # 선택적(extra) 의존성 제외
            dep = _req_name(req)
            if dep and _canon(dep) not in seen:
                stack.append(dep)

    blocks, hashed, skipped = [], 0, []
    for name in sorted(installed, key=str.lower):
        ver = installed[name]
        digs = []
        try:
            r = requests.get(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=15)
            if r.status_code == 200:
                digs_set = {u["digests"]["sha256"] for u in r.json().get("urls", [])
                            if u.get("digests", {}).get("sha256")}
                digs = sorted(digs_set)
        except Exception:
            digs = []
        if digs:
            blocks.append(f"{name}=={ver} \\\n" +
                          " \\\n".join(f"    --hash=sha256:{d}" for d in digs))
            hashed += 1
        else:
            blocks.append(f"# {name}=={ver}  # ⚠️ 해시 조회 실패 — 수동 확인")
            skipped.append(name)
    with open(LOCKFILE, "w", encoding="utf-8") as f:
        f.write("# 자동 생성 (security_lock.py) — 공급망 해시 핀\n")
        f.write("# 설치: pip install --require-hashes -r requirements.lock\n\n")
        f.write("\n".join(blocks) + "\n")
    msg = f"의존성 락: {hashed}개 해시 핀 → {LOCKFILE}"
    if skipped:
        msg += f" (해시 실패 {len(skipped)}: {', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''})"
    print(msg)


def cmd_generate():
    generate_manifest()
    generate_lock()
    print("✅ 기준선 갱신 완료 — security_manifest.json + requirements.lock 를 커밋하세요.")


def cmd_verify() -> int:
    ok, mm = verify_files()
    if ok:
        print("✅ 무결성 검증 통과 — 변조 없음")
        return 0
    print(f"🚨 무결성 위반 {len(mm)}건 (악성 변형/삽입 의심):")
    for rel, why in mm[:50]:
        print(f"  [{why}] {rel}")
    if len(mm) > 50:
        print(f"  ... 외 {len(mm) - 50}건")
    return 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "generate":
        reexec_code = _reexec_generate_with_lock_python()
        if reexec_code is not None:
            sys.exit(reexec_code)
        cmd_generate()
    elif mode == "manifest":
        # 매니페스트만 갱신(네트워크 불필요) — pre-commit 훅 자동 갱신용.
        # requirements.lock(PyPI 조회 필요)는 건드리지 않는다.
        generate_manifest()
    elif mode == "verify":
        sys.exit(cmd_verify())
    else:
        print("사용법: python3 security_lock.py [generate|manifest|verify]")
        sys.exit(2)
