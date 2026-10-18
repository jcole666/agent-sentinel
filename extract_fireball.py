# -*- coding: utf-8 -*-
"""
从你本机的 Codex 桌面版安装里提取 Fireball 宠物精灵图。

用法:
    python extract_fireball.py

会在本目录生成 fireball.png（供 sentinel.py 使用）。
原理：解析 app.asar 头部 JSON，按 offset+size 原样拷贝 webp 字节，再用 Pillow 转 PNG。
"""
import json
import os
import struct
import sys
import glob

OUT_WEBP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fireball.webp")
OUT_PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fireball.png")
TARGET_SUFFIX = "fireball-spritesheet"


def find_codex_asar():
    patterns = [
        r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\app.asar",
        os.path.expanduser(r"~\.codex\**\app.asar"),
    ]
    for pat in patterns:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return sorted(hits)[-1]
    return None


def extract(asar_path):
    with open(asar_path, "rb") as f:
        head = f.read(16)
        header_size = struct.unpack("<I", head[4:8])[0]
        json_len = struct.unpack("<I", head[12:16])[0]
        f.seek(16)
        header = json.loads(f.read(json_len))
        data_start = 8 + header_size

        found = []

        def walk(node, path=""):
            for k, v in node.get("files", {}).items():
                p = f"{path}/{k}"
                if TARGET_SUFFIX in k and k.endswith(".webp"):
                    found.append((p, int(v["size"]), int(v["offset"])))
                walk(v, p)
        walk(header)

        if not found:
            print("未在 asar 中找到 fireball 精灵图（Codex 版本可能不含内置宠物）")
            sys.exit(1)
        path, size, off = found[-1]
        print("FOUND:", path, size, "bytes")
        f.seek(data_start + off)
        blob = f.read(size)
    with open(OUT_WEBP, "wb") as g:
        g.write(blob)
    return OUT_WEBP


def webp_to_png(webp_path):
    try:
        from PIL import Image
    except ImportError:
        print("缺少 Pillow，请先运行: pip install pillow")
        sys.exit(1)
    im = Image.open(webp_path)
    print("图集尺寸:", im.size, im.mode)
    im.save(OUT_PNG)
    print("已生成:", OUT_PNG)


if __name__ == "__main__":
    asar = find_codex_asar()
    if not asar:
        print("找不到 Codex 桌面版的 app.asar（请先安装 Codex 桌面版）")
        sys.exit(1)
    print("asar:", asar)
    webp_to_png(extract(asar))
