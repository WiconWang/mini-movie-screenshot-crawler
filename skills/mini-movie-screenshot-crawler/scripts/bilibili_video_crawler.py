#!/usr/bin/env python3
"""bilibili 游戏视频采集器

查找并下载 bilibili 游戏剧情实录视频的最高清晰度原盘。
分P索引缓存至 .cache/index/ 供关键词快速检索；下载依赖 yt-dlp + ffmpeg，
登录态从 Firefox profile 的 cookies.sqlite 导出（解锁高清晰度）。

用法:
  # 环境自检：yt-dlp / ffmpeg / Firefox 登录态
  python3 bilibili_video_crawler.py check

  # 抓取分P索引并缓存（7 天内重复执行直接命中缓存）
  python3 bilibili_video_crawler.py index <URL或BV号> [--refresh]

  # 在缓存索引中按关键词查找（多词 AND 匹配，不产生网络请求）
  python3 bilibili_video_crawler.py search 盛夏 海岛 [--json]

  # 下载指定分P最高清晰度，自动归档命名
  python3 bilibili_video_crawler.py download BV1Zp4y187oL -p 157 158 \
      --game genshin [-o downloads] [--series 盛夏海岛大冒险]
"""

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = REPO_ROOT / ".cache"
INDEX_DIR = CACHE_DIR / "index"
COOKIES_PATH = CACHE_DIR / "cookies.txt"

COOKIES_MAX_AGE = 6 * 3600          # cookies.txt 复用有效期
INDEX_MAX_AGE = 7 * 24 * 3600       # 索引缓存新鲜期

VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com/",
}

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def die(msg, code=1):
    print("[FAIL] %s" % msg, file=sys.stderr)
    sys.exit(code)


# ============================================================
# Firefox cookies 层
# ============================================================

def find_cookies_db():
    """定位 Firefox profile 的 cookies.sqlite，返回 mtime 最新的一个"""
    env_dir = os.environ.get("FIREFOX_PROFILE_DIR")
    patterns = []
    if env_dir:
        patterns.append(os.path.join(env_dir, "cookies.sqlite"))
    patterns += [
        os.path.expanduser("~/.mozilla/firefox/*/cookies.sqlite"),           # 原生 Linux
        "/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite",  # WSL 读 Windows 侧
    ]
    cands = []
    for pat in patterns:
        cands += [p for p in glob.glob(pat) if os.path.exists(p)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def export_cookies(force=False):
    """从 cookies.sqlite 导出 Netscape 格式 cookies.txt（仅 bilibili 域），返回路径"""
    if COOKIES_PATH.exists() and not force and \
            time.time() - COOKIES_PATH.stat().st_mtime < COOKIES_MAX_AGE:
        return str(COOKIES_PATH)

    src = find_cookies_db()
    if not src:
        die("未找到 Firefox cookies.sqlite；请确认已安装 Firefox 并登录 bilibili，"
            "或用环境变量 FIREFOX_PROFILE_DIR 指定 profile 目录")
    if time.time() - os.path.getmtime(src) > 30 * 24 * 3600:
        print("[WARN] cookies.sqlite 已超过 30 天未更新，登录态可能过期", file=sys.stderr)

    tmpdir = tempfile.mkdtemp(prefix="bili_cookies_")
    dst = os.path.join(tmpdir, "cookies.sqlite")
    for suffix in ("", "-wal", "-shm"):     # WAL 三件套一起拷贝，避免 Firefox 运行中读到不一致状态
        s = src + suffix
        if os.path.exists(s):
            shutil.copy2(s, dst + suffix)

    con = sqlite3.connect(dst)
    rows = con.execute(
        "SELECT host, name, value, path, expiry, isSecure, isHttpOnly "
        "FROM moz_cookies WHERE host LIKE '%bilibili%'"
    ).fetchall()
    con.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    if not any(r[1] == "SESSDATA" for r in rows):
        die("cookies 中无 bilibili SESSDATA——Firefox 可能未登录 bilibili，请先在浏览器登录")

    lines = ["# Netscape HTTP Cookie File"]
    for host, name, value, path, expiry, secure, httponly in rows:
        flag = ("#HttpOnly_" if httponly else "") + host
        sub = "TRUE" if host.startswith(".") else "FALSE"
        lines.append("\t".join([flag, sub, path,
                                "TRUE" if secure else "FALSE",
                                str(expiry), name, value]))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(COOKIES_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(COOKIES_PATH, 0o600)
    except OSError:
        pass
    return str(COOKIES_PATH)


def read_cookie_pairs(cookies_path):
    pairs = {}
    with open(cookies_path, encoding="utf-8") as f:
        for line in f:
            # "#HttpOnly_域名" 是 Netscape 格式的数据行而非注释
            if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 7:
                pairs[parts[5]] = parts[6]
    return pairs


def check_login(cookies_path):
    """校验 bilibili 登录态与会员权限，返回 (ok, 描述)"""
    pairs = read_cookie_pairs(cookies_path)
    if "SESSDATA" not in pairs:
        return False, "导出文件中无 SESSDATA"
    try:
        req = urllib.request.Request(
            NAV_API, headers=dict(HEADERS, Cookie="; ".join(
                "%s=%s" % kv for kv in pairs.items())))
        data = json.load(urllib.request.urlopen(req, timeout=15)).get("data") or {}
        if not data.get("isLogin"):
            return False, "bilibili 接口判定未登录（SESSDATA 可能已失效）"
        vip = "大会员" if data.get("vipStatus") else "普通账号"
        return True, "登录态有效：%s（%s）" % (data.get("uname"), vip)
    except Exception as e:      # 网络异常时降级为弱校验
        return True, "SESSDATA 存在（接口校验失败：%s）" % e


# ============================================================
# 分P索引层
# ============================================================

def extract_bvid(s):
    m = BV_RE.search(s)
    return m.group(0) if m else None


def index_path(bvid):
    return INDEX_DIR / ("%s.json" % bvid)


def fetch_pages_via_api(bvid):
    req = urllib.request.Request(VIEW_API.format(bvid=bvid), headers=HEADERS)
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if d.get("code") != 0:
        raise RuntimeError("view API code=%s (%s)" % (d.get("code"), d.get("message")))
    v = d["data"]
    pages = [{"p": pg["page"], "title": pg["part"], "duration": pg["duration"]}
             for pg in v.get("pages", [])]
    return {"bvid": bvid, "title": v.get("title", ""), "url": _clean_url(v.get("bvid", bvid)),
            "pages": pages}


def fetch_pages_via_ytdlp(bvid):
    """兜底：yt-dlp flat-playlist 拉取分P列表"""
    exe = shutil.which("yt-dlp") or die("yt-dlp 未安装")
    r = subprocess.run(
        [exe, "--flat-playlist", "-J", "--no-color",
         "https://www.bilibili.com/video/%s" % bvid],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp flat-playlist 失败: %s" % r.stderr.strip()[-300:])
    d = json.loads(r.stdout)
    pages = []
    for i, e in enumerate(d.get("entries") or [], start=1):
        pages.append({"p": i, "title": e.get("title") or ("P%d" % i),
                      "duration": int(e.get("duration") or 0)})
    return {"bvid": bvid, "title": d.get("title") or "", "url": _clean_url(bvid),
            "pages": pages}


def _clean_url(bvid):
    return "https://www.bilibili.com/video/%s" % bvid


def load_index(bvid, refresh=False):
    """读取或抓取分P索引，写入缓存后返回 dict"""
    ip = index_path(bvid)
    if ip.exists() and not refresh:
        try:
            cache = json.loads(ip.read_text(encoding="utf-8"))
            age = time.time() - cache.get("fetched_at", 0)
            if age < INDEX_MAX_AGE:
                print("[OK] 命中缓存（%d 小时前更新）：%s《%s》%dP" % (
                    age // 3600, bvid, cache.get("title", "?"), len(cache.get("pages", []))))
                return cache
            print("[INFO] 缓存已过期（%d 天前），重新抓取" % (age // 86400))
        except (ValueError, KeyError):
            pass

    try:
        data = fetch_pages_via_api(bvid)
        via = "view API"
    except Exception as e:
        print("[WARN] view API 失败（%s），改用 yt-dlp 兜底" % e, file=sys.stderr)
        data = fetch_pages_via_ytdlp(bvid)
        via = "yt-dlp"

    if not data["pages"]:
        die("未能获取任何分P信息：%s" % bvid)

    data["fetched_at"] = time.time()
    data["fetched_via"] = via
    data["page_count"] = len(data["pages"])
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(ip) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ip)

    print("[OK] 已缓存 %s《%s》共 %dP（via %s）" % (
        bvid, data["title"], data["page_count"], via))
    print("     缓存文件: %s" % ip)
    return data


# ============================================================
# 工具函数
# ============================================================

def fmt_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def sanitize_filename(name):
    """清洗为跨平台安全文件名：去除 ASCII 非法字符，保留中文全角标点"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80]


def require_exe(name, purpose):
    p = shutil.which(name)
    if not p:
        die("%s 未安装（%s 需要）" % (name, purpose))
    return p


# ============================================================
# 子命令
# ============================================================

def cmd_check(_args):
    fails = 0

    ytdlp = shutil.which("yt-dlp")
    if ytdlp:
        ver = subprocess.run([ytdlp, "--version"], capture_output=True,
                             text=True).stdout.strip()
        print("[OK] yt-dlp %s (%s)" % (ver, ytdlp))
    else:
        print("[FAIL] yt-dlp 未安装")
        fails += 1

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        ver = subprocess.run([ffmpeg, "-version"], capture_output=True,
                             text=True).stdout.splitlines()[0]
        print("[OK] %s (%s)" % (ver.split()[2] if len(ver.split()) > 2 else ver, ffmpeg))
    else:
        print("[FAIL] ffmpeg/ffprobe 未安装（音视频合并必需）")
        fails += 1

    db = find_cookies_db()
    if db:
        print("[OK] Firefox profile: %s" % db)
        try:
            cp = export_cookies()
            ok, msg = check_login(cp)
            print("[%s] 登录态: %s" % ("OK" if ok else "FAIL", msg))
            fails += 0 if ok else 1
            print("[OK] cookies 已就绪: %s" % cp)
        except SystemExit:
            print("[FAIL] cookies 导出失败（详见上方错误）")
            fails += 1
    else:
        print("[FAIL] 未找到 Firefox cookies.sqlite（需安装 Firefox 并登录 bilibili）")
        fails += 1

    usage = shutil.disk_usage(REPO_ROOT)
    free_gb = usage.free / 1024 ** 3
    print("[INFO] 仓库所在磁盘剩余空间: %.1f GB" % free_gb)
    if free_gb < 10:
        print("[WARN] 剩余空间不足 10GB，批量下载大体积视频前请清理")

    print("=" * 40)
    if fails:
        print("自检未通过，%d 项 FAIL" % fails)
        sys.exit(1)
    print("自检通过，可以下载")


def cmd_index(args):
    bvid = extract_bvid(args.url_or_bv) or die("无法从输入提取 BV 号: %s" % args.url_or_bv)
    load_index(bvid, refresh=args.refresh)


def cmd_search(args):
    kws = [k.lower() for k in args.keywords]
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    hits = 0
    for ip in sorted(INDEX_DIR.glob("*.json")):
        cache = json.loads(ip.read_text(encoding="utf-8"))
        matched = [
            pg for pg in cache.get("pages", [])
            if all(k in pg["title"].lower() for k in kws)
        ]
        if not matched:
            continue
        hits += len(matched)
        dt = datetime.fromtimestamp(cache.get("fetched_at", 0)).strftime("%Y-%m-%d %H:%M")
        print("\n[%s] 《%s》 %dP（索引时间 %s）" % (
            cache["bvid"], cache.get("title", "?"),
            cache.get("page_count", len(cache.get("pages", []))), dt))
        for pg in matched:
            row = {
                "bvid": cache["bvid"],
                "video_title": cache.get("title", ""),
                "p": pg["p"],
                "title": pg["title"],
                "duration_hms": fmt_duration(pg["duration"]),
                "url": "%s?p=%d" % (_clean_url(cache["bvid"]), pg["p"]),
            }
            if args.json:
                print(json.dumps(row, ensure_ascii=False))
            else:
                print("  p%-4d %-46s %10s" % (row["p"], row["title"][:46], row["duration_hms"]))
                print("        %s" % row["url"])
    if not hits:
        print("未命中。可尝试先刷新基准实录的索引：index <URL> --refresh")
        sys.exit(2)


def cmd_download(args):
    bvid = extract_bvid(args.url_or_bv) or die("无法从输入提取 BV 号: %s" % args.url_or_bv)
    cache = load_index(bvid)
    pages_by_no = {pg["p"]: pg for pg in cache["pages"]}
    missing = [p for p in args.pages if p not in pages_by_no]
    if missing:
        die("分P不存在于索引: %s（该视频共 %dP）" % (
            missing, cache["page_count"]))

    require_exe("yt-dlp", "下载")
    require_exe("ffmpeg", "合并音视频")
    cookies_path = export_cookies(force=args.fresh_cookies)

    series = sanitize_filename(args.series) if args.series \
        else sanitize_filename(cache["title"])
    target_dir = Path(args.out_dir) / args.game / series
    target_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for p in sorted(set(args.pages)):
        info = pages_by_no[p]
        final_name = "%03d-%s.mp4" % (p, sanitize_filename(info["title"]))
        final_path = target_dir / final_name
        source_url = "%s?p=%d" % (_clean_url(bvid), p)

        if final_path.exists() and not args.force:
            size_mb = final_path.stat().st_size / 1024 / 1024
            print("[SKIP] 已存在: %s (%.1f MB)" % (final_path, size_mb))
            results.append({"file": str(final_path), "size_mb": round(size_mb, 1),
                            "resolution": None, "source_url": source_url,
                            "skipped": True, "failed": False})
            continue

        workdir = target_dir / (".part-p%d" % p)
        workdir.mkdir(parents=True, exist_ok=True)
        fmt = "bv*[height<=%d]+ba/b[height<=%d]/bv*+ba/b" % (args.max_height, args.max_height) \
            if args.max_height else "bv*+ba/b"
        cmd = [
            shutil.which("yt-dlp"),
            "--cookies", cookies_path,
            "--no-playlist", "--no-color", "--newline",
            "-f", fmt,
            "-S", "res,vcodec:avc1,acodec:aac,br",  # h264 裸字段排序已废弃；音频编码改用 acodec:aac
            "--merge-output-format", "mp4",
            "--retries", "5", "--fragment-retries", "5",
            "-o", str(workdir / "dl.%(ext)s"),
            source_url,
        ]
        print("[DL ] p%d %s -> %s" % (p, info["title"], final_name))
        rc = subprocess.run(cmd).returncode
        merged = workdir / "dl.mp4"
        if rc != 0 or not merged.exists():
            print("[FAIL] p%d 下载失败（.part-p%d 目录已保留供续传）" % (p, p), file=sys.stderr)
            results.append({"file": str(final_path), "page": p, "page_title": info["title"],
                            "size_mb": None, "resolution": None, "source_url": source_url,
                            "skipped": False, "failed": True})
            continue   # 记录失败但不中断批次，其余分P继续下载

        resolution = probe_resolution(merged)
        shutil.move(str(merged), str(final_path))
        shutil.rmtree(workdir, ignore_errors=True)

        meta = {
            "source_url": source_url,
            "bvid": bvid,
            "page": p,
            "page_title": info["title"],
            "video_title": cache["title"],
            "duration_sec": info["duration"],
            "resolution": resolution,
            "size_bytes": final_path.stat().st_size,
            "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (target_dir / (final_name[:-4] + ".mp4.meta.json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

        size_mb = meta["size_bytes"] / 1024 / 1024
        print("[OK ] p%d 完成: %s (%.1f MB%s)" % (
            p, final_path, size_mb,
            ", %s" % resolution if resolution else ""))
        results.append({"file": str(final_path), "size_mb": round(size_mb, 1),
                        "resolution": resolution, "source_url": source_url,
                        "skipped": False, "failed": False})

    print("\n=== DOWNLOAD RESULT (JSON) ===")
    print(json.dumps({"game": args.game, "series": series,
                      "video_title": cache["title"], "items": results},
                     ensure_ascii=False, indent=1))

    failed = [r for r in results if r.get("failed")]
    if failed:
        print("[FAIL] %d 个分P下载失败: %s（失败项可用 --force 单独重试，.part-pX 目录已保留供续传）" % (
            len(failed), [r["page"] for r in failed]), file=sys.stderr)
        sys.exit(1)


def probe_resolution(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        st = json.loads(r.stdout)["streams"][0]
        return "%dx%d" % (st["width"], st["height"])
    except Exception:
        return None


# ============================================================
# 入口
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="bilibili 游戏视频采集器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="环境自检：yt-dlp / ffmpeg / Firefox 登录态")

    p = sub.add_parser("index", help="抓取分P索引并缓存")
    p.add_argument("url_or_bv", help="视频 URL 或 BV 号")
    p.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")

    p = sub.add_parser("search", help="在缓存索引中按关键词查找")
    p.add_argument("keywords", nargs="+", help="关键词（多个为 AND 匹配）")
    p.add_argument("--json", action="store_true", help="以 JSONL 输出结果")

    p = sub.add_parser("download", help="下载指定分P最高清晰度")
    p.add_argument("url_or_bv", help="视频 URL 或 BV 号")
    p.add_argument("-p", "--pages", type=int, nargs="+", required=True,
                   help="要下载的分P号（可多个）")
    p.add_argument("-o", "--out-dir", default=str(REPO_ROOT / "downloads"),
                   help="输出根目录（默认 <仓库>/downloads）")
    p.add_argument("--game", default="genshin", help="游戏分类目录名（默认 genshin）")
    p.add_argument("--series", default=None,
                   help="剧集子目录名（默认使用视频标题）")
    p.add_argument("--max-height", type=int, default=None,
                   help="限制最大分辨率高度（默认不限制，取账号可用最高清）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p.add_argument("--fresh-cookies", action="store_true", help="强制重新导出 cookies")

    args = ap.parse_args()
    {"check": cmd_check, "index": cmd_index,
     "search": cmd_search, "download": cmd_download}[args.cmd](args)


if __name__ == "__main__":
    main()
