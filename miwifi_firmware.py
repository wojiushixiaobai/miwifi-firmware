#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miwifi_firmware.py —— 小米路由器（MiWiFi）固件抓取/下载工具

数据源:
    https://api.miwifi.com/upgrade/log/list?typeList=<型号代号>

型号代号 = 设备型号 + 渠道后缀，例如 RC01STA / R3GDEV / R2DUSB / R1DIP。
后缀含义（已知）: STA=稳定版, DEV=开发版, USB=USB 恢复/刷机包,
                  IP=iPhone 客户端, MAC=Mac 客户端, PC=PC 客户端,
                  AN Android 客户端, DIP=?, 以及 -TW/-HK 地区版。

用法:
    python3 miwifi_firmware.py                    # 下载全部已知型号
    python3 miwifi_firmware.py --json-only        # 只保存 JSON
    python3 miwifi_firmware.py -t RC01STA,R3GDEV  # 只处理指定型号
    python3 miwifi_firmware.py --discover         # 先遍历发现型号，再下载
    python3 miwifi_firmware.py --discover-only    # 只发现型号，写入 types.txt

产物:
    <out>/<型号代号>/<型号代号>.json        # 接口原始 JSON，完整保存
    <out>/<型号代号>/<版本>/<固件文件>       # 固件本体
    <out>/_failed.txt                       # 本次失败的条目（如有）
"""

import argparse
import concurrent.futures as futures
import itertools
import json
import os
import queue
import re
import string
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.miwifi.com/upgrade/log/list"
USER_AGENT = "Mozilla/5.0 (compatible; miwifi-firmware/1.0)"
MAX_TYPE_LIST_CHARS = 4800  # 单次请求 typeList 的字符上限（服务端 URL 上限约 6K）

# 已知型号代号（由官方下载页 + 接口遍历得到，可按需增删；也可用 --types-file 覆盖）
KNOWN_TYPES = [
    "D01STA",
    "R1350STA",
    "R1CDEV", "R1CLDEV", "R1CLSTA", "R1CSTA", "R1CUSB",
    "R1DANDEV", "R1DANSTA", "R1DDEV", "R1DIP", "R1DMAC", "R1DPC",
    "R1DSTA", "R1DUSB", "R1DUSB2",
    "R2100STA", "R2350STA",
    "R2DDEV", "R2DSTA", "R2DUSB",
    "R3600STA",
    "R3ASTA", "R3DDEV", "R3DEV", "R3DSTA",
    "R3GDEV", "R3GSTA", "R3LDEV", "R3LSTA",
    "R3PDEV", "R3PSTA", "R3STA",
    "R3STA-HK", "R3STA-TW", "R3PSTA-TW",
    "R4ACSTA", "R4ACSTA-220", "R4ASTA", "R4ASTA-230", "R4CMSTA", "R4CSTA", "R4STA",
    "R1CSTA-HK", "R1CSTA-TW",
    "RA67STA", "RA69STA", "RA70DEV", "RA70STA", "RA71STA", "RA72STA",
    "RA74STA", "RA80STA", "RA81STA",
    "RB03STA", "RB04STA", "RB06STA", "RB08STA",
    "RC01STA", "RC06STA",
    "RD01STA", "RD02STA", "RD03STA", "RD04STA", "RD08STA", "RD15STA", "RD16STA", "RD18STA",
    "RM1800STA", "RM2100STA",
    "TFTPPC", "WiFiAN", "WiFiPC",
]

# 工具/客户端类代号：官方公开下载，默认不归档（--include-tools 可强制包含）
TOOL_TYPES = {
    "TFTPPC", "WiFiAN", "WiFiPC",
    "R1DIP", "R1DMAC", "R1DPC", "R1DANSTA", "R1DANDEV",
}

# 遍历用：已知的渠道后缀（STA/DEV/USB 是固件，其余是客户端/工具）
DEVICE_SUFFIXES = ["STA", "DEV", "USB", "DIP", "IP", "MAC", "PC", "AN", "TFTP", "WiFi"]
# 新设备型号探测时只尝试与固件相关的后缀
FIRMWARE_SUFFIXES = ["STA", "DEV"]
# 境外版本只探测台湾（官方海外站仅此一个）
REGION_SUFFIXES = ["TW"]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url, timeout=60, retries=3):
    """返回响应 body(bytes)；失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = e
            # 4xx（除 429）重试无意义
            if 400 <= e.code < 500 and e.code != 429:
                raise
        except Exception as e:  # noqa: BLE001 网络波动统一重试
            last = e
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last


def query_types(codes, timeout=60, retries=3):
    """批量查询型号代号，返回匹配到的 realType 集合。"""
    found = set()
    for chunk in chunk_by_len(codes, MAX_TYPE_LIST_CHARS):
        url = API_BASE + "?" + urllib.parse.urlencode({"typeList": ",".join(chunk)})
        try:
            payload = json.loads(http_get(url, timeout, retries).decode("utf-8"))
        except Exception:
            continue
        if payload.get("code") == 0:
            found.update(item["realType"] for item in payload["data"]["list"])
    return found


def chunk_by_len(items, limit):
    buf, size = [], 0
    for it in items:
        if buf and size + len(it) + 1 > limit:
            yield buf
            buf, size = [], 0
        buf.append(it)
        size += len(it) + 1
    if buf:
        yield buf


# --------------------------------------------------------------------------- #
# 型号发现（可选）
# --------------------------------------------------------------------------- #
def _seed_devices():
    """从已知型号代号中还原设备型号（去掉渠道后缀）。"""
    devices = set()
    for code in KNOWN_TYPES:
        base = code.split("-")[0]
        for suf in sorted(DEVICE_SUFFIXES, key=len, reverse=True):
            if base.endswith(suf) and len(base) > len(suf):
                base = base[: -len(suf)]
                break
        if base.startswith("R") and len(base) <= 8 and base not in KNOWN_TYPES:
            devices.add(base)
    return devices


def _model_namespace():
    """设备型号命名空间（收敛后的可枚举集合）。"""
    models = set()
    for n in range(1, 5):  # R1..R9999
        for digits in itertools.product(string.digits, repeat=n):
            models.add("R" + "".join(digits))
    for n in (1, 2):  # R3G / R4AC / R4CM
        for digits in itertools.product(string.digits, repeat=n):
            for m in (1, 2):
                for letters in itertools.product(string.ascii_uppercase, repeat=m):
                    models.add("R" + "".join(digits) + "".join(letters))
    for letter in "ABCDM":  # RA70 / RB03 / RC01 / RD03 / RM2100
        for n in (2, 4):
            for digits in itertools.product(string.digits, repeat=n):
                models.add("R" + letter + "".join(digits))
    return models


def discover(verbose=True):
    """遍历型号代号，返回经二次验证的有效代号列表。

    候选来源（均来自已有设备信息拼接，境外只探测 -TW）:
      1. 已知代号 + 全部已知渠道后缀；
      2. 已知设备型号 + 已知后缀；
      3. 设备型号命名空间 + 固件后缀（STA/DEV/USB）；
      4. 已知代号 + "-TW"。
    """
    candidates = set(KNOWN_TYPES)
    devices = _seed_devices()
    for model in devices:
        for suf in DEVICE_SUFFIXES:
            candidates.add(model + suf)
    for model in _model_namespace():
        for suf in FIRMWARE_SUFFIXES:
            candidates.add(model + suf)
    for code in KNOWN_TYPES:
        for region in REGION_SUFFIXES:
            candidates.add(code + "-" + region)

    if verbose:
        print(f"[discover] 候选 {len(candidates)} 个，开始查询……", flush=True)

    valid, lock = set(), threading.Lock()
    tasks = queue.Queue()
    for chunk in chunk_by_len(sorted(candidates), MAX_TYPE_LIST_CHARS):
        tasks.put(chunk)

    def worker():
        while True:
            try:
                chunk = tasks.get_nowait()
            except queue.Empty:
                return
            got = query_types(chunk)
            with lock:
                valid.update(got)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if verbose:
        print(f"[discover] 初筛有效 {len(valid)} 个，耗时 {time.time() - t0:.1f}s", flush=True)

    # 二次验证：逐个代号确认接口确实返回该 realType
    confirmed = set()
    for code in sorted(valid):
        try:
            got = query_types([code])
        except Exception:
            got = set()
        if code in got:
            confirmed.add(code)
        elif verbose:
            print(f"[discover] 验证失败，丢弃 {code}", flush=True)
    if verbose:
        print(f"[discover] 验证通过 {len(confirmed)} 个", flush=True)
    return sorted(confirmed)


# --------------------------------------------------------------------------- #
# 抓取 + 下载
# --------------------------------------------------------------------------- #
def safe_name(text, fallback="unknown"):
    text = (text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text)
    return text or fallback


def head_size(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def download(url, dest, resume=True):
    """下载到 dest；已存在且大小一致则跳过。返回 (状态, 字节数)。

    状态: ok=已下载, skip=已存在, missing=源站已删除(404/410)。
    """
    try:
        return _download(url, dest, resume)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 410):
            return "missing", 0
        raise


def _download(url, dest, resume=True):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    remote = head_size(url) if os.path.exists(dest) else 0
    if os.path.exists(dest):
        local = os.path.getsize(dest)
        if remote and local == remote:
            return "skip", local
        if remote and local != remote:
            os.remove(dest)

    offset = os.path.getsize(tmp) if resume and os.path.exists(tmp) else 0
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            if offset and resp.status != 206:  # 服务端不支持续传
                offset = 0
            total = (offset + int(resp.headers.get("Content-Length") or 0)) if offset else int(resp.headers.get("Content-Length") or 0)
            with open(tmp, "ab" if offset else "wb") as fh:
                while True:
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    fh.write(block)
    except Exception:
        raise
    size = os.path.getsize(tmp)
    if total and size != total:
        raise IOError(f"size mismatch: got {size}, expected {total}")
    os.replace(tmp, dest)
    return "ok", size


def fetch_one_type(code, out_dir, timeout, retries):
    """抓取单个型号代号，保存原始 JSON，返回 (code, list, raw_bytes)。"""
    url = API_BASE + "?" + urllib.parse.urlencode({"typeList": code})
    raw = http_get(url, timeout, retries)
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("code") != 0:
        raise ValueError(f"接口返回异常: {payload.get('code')}")
    items = payload["data"]["list"]
    if not items:
        raise ValueError("接口返回空列表")
    type_dir = os.path.join(out_dir, code)
    os.makedirs(type_dir, exist_ok=True)
    json_path = os.path.join(type_dir, code + ".json")
    tmp = json_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)  # 原样保存，保证字段完整
    os.replace(tmp, json_path)
    return code, items, raw


def run(codes, out_dir, workers=4, json_only=False, force=False, timeout=60, retries=3,
        skip_names=None):
    os.makedirs(out_dir, exist_ok=True)
    stats = {"types": 0, "files": 0, "skipped": 0, "missing": 0,
             "json_failed": [], "dl_failed": []}
    downloads = []  # (dest, url, label)

    for i, code in enumerate(codes, 1):
        try:
            _, items, _ = fetch_one_type(code, out_dir, timeout, retries)
        except Exception as e:  # noqa: BLE001
            print(f"[JSON-FAIL] {code}: {e}", flush=True)
            stats["json_failed"].append(f"{code}\t{e}")
            continue
        stats["types"] += 1
        print(f"[JSON] {i}/{len(codes)} {code}: {len(items)} 条版本", flush=True)
        if json_only:
            continue
        for item in items:
            url = (item.get("url") or "").strip()
            # 个别 URL 内含零宽字符等非 ASCII 字符，urllib 会直接报错
            url = "".join(ch for ch in url if 32 < ord(ch) < 127)
            if not url:
                continue
            version = safe_name(item.get("version"))
            filename = safe_name(os.path.basename(urllib.parse.urlparse(url).path), "firmware.bin")
            dest = os.path.join(out_dir, code, version, filename)
            downloads.append((dest, url, f"{code} {version}"))

    if json_only or not downloads:
        return stats

    skip_names = skip_names or set()
    seen = set()
    unique = []
    for dest, url, label in downloads:
        if dest in seen:
            continue
        seen.add(dest)
        if os.path.basename(dest) in skip_names and not force:
            stats["skipped"] += 1
            continue
        unique.append((dest, url, label))
    print(f"[DL] 待下载文件 {len(unique)} 个，并发 {workers}", flush=True)

    def job(dest, url):
        if force and os.path.exists(dest):
            os.remove(dest)
        return download(url, dest)

    done = 0
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        mapping = {pool.submit(job, d, u): (d, label) for d, u, label in unique}
        for fut in futures.as_completed(mapping):
            dest, label = mapping[fut]
            done += 1
            try:
                status, size = fut.result()
                if status == "skip":
                    stats["skipped"] += 1
                elif status == "missing":
                    stats["missing"] += 1
                    print(f"[缺失] {label} 源站已删除", flush=True)
                else:
                    stats["files"] += 1
                print(f"[{done}/{len(unique)}] {label} -> {os.path.relpath(dest, out_dir)} ({size} B)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[DL-FAIL] {label}: {e}", flush=True)
                stats["dl_failed"].append(f"{label}\t{url}\t{e}")

    for fname, rows in (("_json_failed.txt", stats["json_failed"]), ("_dl_failed.txt", stats["dl_failed"])):
        path = os.path.join(out_dir, fname)
        if rows:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + "\n")
        elif os.path.exists(path):
            os.remove(path)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description="小米路由器固件抓取/下载")
    parser.add_argument("-o", "--out", default="firmware", help="输出目录（默认 ./firmware）")
    parser.add_argument("-t", "--types", help="逗号分隔的型号代号，优先于 types.txt")
    parser.add_argument("--types-file", default="types.txt", help="型号代号列表文件（每行一个）")
    parser.add_argument("--json-only", action="store_true", help="只保存 JSON，不下载固件")
    parser.add_argument("--discover", action="store_true", help="先遍历发现型号，合并后下载")
    parser.add_argument("--discover-only", action="store_true", help="只遍历发现型号，写入 types.txt")
    parser.add_argument("-j", "--workers", type=int, default=4, help="下载并发数（默认 4）")
    parser.add_argument("--force", action="store_true", help="强制重新下载已存在的固件")
    parser.add_argument("--include-tools", action="store_true", help="连同工具/客户端类代号一起下载（默认忽略）")
    parser.add_argument("--skip-existing-list", help="文件名列表（每行一个），命中则跳过下载（用于跳过 release 已有资产）")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP 超时秒数")
    parser.add_argument("--retries", type=int, default=3, help="重试次数")
    args = parser.parse_args(argv)

    if args.types:
        codes = [c.strip() for c in args.types.split(",") if c.strip()]
    elif os.path.exists(args.types_file):
        with open(args.types_file, encoding="utf-8") as fh:
            codes = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    else:
        codes = list(KNOWN_TYPES)

    if args.discover or args.discover_only:
        found = discover()
        # 合并旧清单后再次验证，剔除接口已下线的代号
        merged = sorted(set(codes) | set(found))
        confirmed = query_types(merged)
        dropped = sorted(set(merged) - confirmed)
        merged = [code for code in merged if code in confirmed]
        if dropped:
            print(f"[discover] 剔除失效代号: {', '.join(dropped)}", flush=True)
        with open(args.types_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(merged) + "\n")
        print(f"[discover] 已写入 {args.types_file}（{len(merged)} 个）", flush=True)
        codes = merged
        if args.discover_only:
            return 0

    if not args.include_tools:
        tools = [c for c in codes if c in TOOL_TYPES]
        if tools:
            print(f"[skip] 忽略工具/客户端类: {', '.join(tools)}", flush=True)
        codes = [c for c in codes if c not in TOOL_TYPES]

    skip_names = set()
    if args.skip_existing_list and os.path.exists(args.skip_existing_list):
        with open(args.skip_existing_list, encoding="utf-8") as fh:
            skip_names = {line.strip() for line in fh if line.strip()}

    stats = run(codes, args.out, workers=args.workers, json_only=args.json_only,
                force=args.force, timeout=args.timeout, retries=args.retries,
                skip_names=skip_names)
    print("\n===== 汇总 =====")
    print(f"型号成功: {stats['types']} / {len(codes)}")
    print(f"固件下载: {stats['files']}，跳过: {stats['skipped']}，"
          f"源站缺失: {stats['missing']}，失败: {len(stats['dl_failed'])}")
    if stats["json_failed"]:
        print(f"JSON 失败: {len(stats['json_failed'])}（见 {os.path.join(args.out, '_json_failed.txt')}）")
    return 0 if not stats["json_failed"] and not stats["dl_failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
