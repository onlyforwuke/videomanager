# -*- coding: utf-8 -*-
import os
import json
import subprocess
import shutil

CONFIG_PATH = r"config.json"
CRF = "21"

# -----------------------------
# ffprobe 检查视频是否正常
# -----------------------------
def is_video_valid(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1:nk=1",
                path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8"
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


# -----------------------------
# 转码为 H.264
# -----------------------------
def convert_to_h264(src: str) -> str | None:
    base, _ = os.path.splitext(src)
    dst = base + "_h264.mkv"

    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        "-c:v", "libx264",
        "-crf", CRF,
        "-preset", "slow",
        "-tune", "animation",
        "-x264-params", "ref=5:bframes=8:b-adapt=2:me=umh:merange=34:subme=10:rc-lookahead=240:trellis=2:aq-mode=3:aq-strength=0.9:psy-rd=1.0:0.15:deblock=-1:-1:qcomp=0.5:partitions=all:no-fast-pskip:direct=auto:scenecut=60",
        "-c:a", "copy",
        "-c:s", "copy",
        "-map_metadata", "0",
        "-map_chapters", "0",
        dst
    ]

    print(f"▶ 转码: {os.path.basename(src)}")

    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="ignore"
    )

    if p.returncode != 0:
        print("❌ ffmpeg 失败")
        return None

    if not os.path.exists(dst) or os.path.getsize(dst) < 1024 * 1024:
        print("❌ 输出文件异常")
        return None

    if not is_video_valid(dst):
        print("❌ 输出视频无法播放")
        return None

    return dst


# -----------------------------
# 主流程
# -----------------------------
def main():
    if not os.path.exists(CONFIG_PATH):
        print("❌ config.json 不存在")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    changed = False

    for path, info in list(config.items()):
        score = info.get("compress_score", 0)

        if score <= 0:
            continue

        if not os.path.exists(path):
            print(f"⚠ 文件不存在，移除记录: {path}")
            del config[path]
            changed = True
            continue

        dst = convert_to_h264(path)
        if not dst:
            continue

        print("✅ 转码成功，验证通过")

        try:
            os.remove(path)
            print("🗑 已删除源文件")
        except Exception as e:
            print("⚠ 删除源文件失败:", e)
            continue

        del config[path]
        changed = True

    if changed:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        shutil.move(tmp, CONFIG_PATH)
        print("✅ config.json 已更新")

    print("🎉 任务完成")


if __name__ == "__main__":
    main()