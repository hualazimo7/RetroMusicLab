"""_scan_joshw_hosts.py -- 扫 joshw.info 各子站根目录，判断「一个站一个机种」还是「一个站多个机种」。

判据：根目录里出现的链接
  * 以 ``/`` 结尾  -> 子目录（可能是机种，也可能只是 lost+found）
  * 以 ``.7z`` 结尾 -> 音源包（说明这个站本身就是单一机种，包直接躺在根下）

用法：
    py -3.14 vendor\\_scan_joshw_hosts.py
"""
import os
import re
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HOSTS = [
    "spc", "nsf", "gbs", "gsf", "smd", "hes", "usf", "ssf", "dsf", "2sf",
    "wsr", "kss", "3do",
    "psf", "psf2", "psf3", "psf4", "psp", "vita",
    "gcn", "wii", "wiiu", "xbox", "x360", "3sf",
    "s98", "hoot", "fmtowns", "pc",
]

LINK = re.compile(r'<a href="([^"]+)">([^<]*)</a>', re.I)


def fetch(url, timeout=25):
    """走 curl —— 本机 urllib 会被代理拦成 502 Tunnel，curl 不受影响。"""
    import subprocess
    r = subprocess.run(
        ["curl", "-sL", "-m", str(timeout), "-A", UA, url],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError("curl exit %d" % r.returncode)
    return r.stdout.decode("utf-8", "replace")


def scan(host):
    """返回 (dirs, n_7z, total_links)。dirs 已剔除 lost+found / 排序链接。"""
    url = "https://%s/" % host
    html = fetch(url)
    dirs, n7z = [], 0
    for href, text in LINK.findall(html):
        t = (text or "").strip()
        if t.startswith("?") or href.startswith("?"):
            continue
        if href.endswith(".7z") or t.endswith(".7z"):
            n7z += 1
        elif t.endswith("/") or href.endswith("/"):
            name = t.rstrip("/") or href.rstrip("/")
            if name.lower() in ("lost+found", "parent directory", "../"):
                continue
            dirs.append(name)
    return dirs, n7z


def main():
    print("%-10s %-6s %-6s %s" % ("host", "dirs", "7z", "子目录（机种）"))
    print("-" * 78)
    rows = []
    for h in HOSTS:
        try:
            dirs, n7z = scan(h)
            rows.append((h, dirs, n7z, ""))
            tail = ", ".join(dirs[:8]) + (" …" if len(dirs) > 8 else "")
            print("%-10s %-6d %-6d %s" % (h + ".joshw.info", len(dirs), n7z, tail))
        except Exception as e:
            rows.append((h, [], 0, repr(e)))
            print("%-10s %-6s %-6s ERROR %s" % (h + ".joshw.info", "-", "-", e))
        time.sleep(0.3)

    print()
    print("=== 一个站多个机种（dirs > 0）===")
    for h, dirs, n7z, err in rows:
        if dirs:
            print("%-22s %d 个目录: %s" % (h + ".joshw.info", len(dirs), ", ".join(dirs)))
    if not any(r[1] for r in rows):
        print("（无）")


if __name__ == "__main__":
    sys.exit(main())
