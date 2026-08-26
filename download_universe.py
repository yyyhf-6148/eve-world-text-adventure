"""下载 EVE 星系地图数据（Fuzzwork CSV）

用法:
    python download_universe.py
"""

import shutil
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_DIR = BASE_DIR / "sde" / "universe"

FILES = {
    "mapSolarSystems.csv": "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystems.csv",
    "mapSolarSystemJumps.csv": "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystemJumps.csv",
}


def download_file(url: str, dest: Path) -> None:
    print(f"下载中: {url}")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(1024 * 256):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            print(f"\r进度: {downloaded / total * 100:.1f}%", end="", flush=True)
                print()
        shutil.move(str(tmp), dest)
        print(f"下载完成: {dest}")
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"下载失败: {e}")


def ensure_universe_data() -> Path:
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        dest = UNIVERSE_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"已存在: {dest}")
            continue
        download_file(url, dest)
    return UNIVERSE_DIR


if __name__ == "__main__":
    ensure_universe_data()
    print(f"星系数据就绪: {UNIVERSE_DIR}")