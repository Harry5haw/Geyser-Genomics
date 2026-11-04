#!/usr/bin/env python3
"""Run the web UI and the local pipeline worker together. One command, no AWS.

    python web/dev.py                 # http://127.0.0.1:8000
    python web/dev.py --port 8080
    python web/dev.py --runner host   # tools on PATH instead of Docker
    python web/dev.py --production --no-worker   # as deployed: DEBUG off, collectstatic, hashed assets

Prerequisite: the pipeline image, built once with  docker build -f Dockerfile -t geyser-app .
Ctrl+C stops both processes. This is a development server (DEBUG on); see the README for
running without DEBUG.
"""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(WEB_DIR)


def image_exists(image):
    if not shutil.which("docker"):
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        == 0
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8000")
    p.add_argument("--root", default=os.path.join(REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--runner", choices=["docker", "host"], default="docker")
    p.add_argument("--image", default="geyser-app")
    p.add_argument("--no-worker", action="store_true", help="UI only; nothing will process uploads")
    p.add_argument(
        "--production",
        action="store_true",
        help="serve as deployed (DEBUG off, collectstatic into a temporary folder: hashed, compressed, immutable "
        "assets), e.g. for Lighthouse; a throwaway secret key unless DJANGO_SECRET_KEY is set",
    )
    a = p.parse_args(argv)

    env = {
        **os.environ,
        "DJANGO_DEBUG": "1",
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_DIR": os.path.abspath(a.root),
        "GEYSER_RUNNER": a.runner,
        "GEYSER_IMAGE": a.image,
        "PYTHONUNBUFFERED": "1",
    }
    env.setdefault("REFERENCE_NAME", "synthetic_ref.fa")
    if a.production:
        env["DJANGO_DEBUG"] = "0"
        env.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(32))
        env["DJANGO_STATIC_ROOT"] = env.get("DJANGO_STATIC_ROOT") or tempfile.mkdtemp(prefix="geyser-static-")
        collect = [sys.executable, os.path.join(WEB_DIR, "manage.py"), "collectstatic", "--noinput", "-v0"]
        subprocess.run(collect, env=env, cwd=REPO_ROOT, check=True)

    if not a.no_worker and a.runner == "docker" and not image_exists(a.image):
        print(
            f"WARNING: Docker image '{a.image}' not found, so pipeline runs will fail.\n"
            f"         Build it first:  docker build -f Dockerfile -t {a.image} .\n",
            flush=True,
        )

    manage = [sys.executable, os.path.join(WEB_DIR, "manage.py")]
    procs = []
    if not a.no_worker:
        procs.append(subprocess.Popen(manage + ["runworker"], env=env, cwd=REPO_ROOT))
    server = ["runserver", f"{a.host}:{a.port}"] + (["--noreload"] if a.production else [])
    procs.append(subprocess.Popen(manage + server, env=env, cwd=REPO_ROOT))
    print(f"\nGeyser Genomics UI: http://{a.host}:{a.port}/   (storage: {env['LOCAL_STORAGE_DIR']})\n", flush=True)

    try:
        while all(proc.poll() is None for proc in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return max((proc.returncode or 0) for proc in procs)


if __name__ == "__main__":
    sys.exit(main())
