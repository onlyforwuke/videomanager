# -*- coding:utf-8 -*-
from __future__ import annotations

import sys
import os
import json
import subprocess
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Optional, TypedDict

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QFileDialog, QTableWidget, QTableWidgetItem,
    QMessageBox, QHBoxLayout, QCheckBox, QComboBox, QLabel, QLineEdit,
    QMenu, QProgressBar, QDialog, QTextEdit
)
import psutil

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# =======================
# 类型定义
# =======================
class VideoInfo(TypedDict):
    name: str
    path: str
    size: int
    mtime: float
    duration: float
    size_mb: float
    mb_per_min: float
    audio_cnt: int
    sub_cnt: int
    codec: str
    bitrate_kbps: int
    compress_score: int
    save_pct: int


@dataclass
class EncoderConfig:
    name: str
    display_name: str
    default_crf: str
    tag: str


ENCODERS: list[EncoderConfig] = [
    EncoderConfig("libx264", "libx264 (H.264)", "23", "x264"),
    EncoderConfig("libx265", "libx265 (H.265/HEVC)", "24", "x265"),
    EncoderConfig("libvpx-vp9", "libvpx-vp9 (VP9)", "30", "vp9"),
    EncoderConfig("libaom-av1", "libaom-av1 (AV1)", "30", "av1"),
    EncoderConfig("h264_nvenc", "h264_nvenc (NVIDIA)", "23", "nvenc_h264"),
    EncoderConfig("hevc_nvenc", "hevc_nvenc (NVIDIA)", "28", "nvenc_h265"),
    EncoderConfig("h264_qsv", "h264_qsv (Intel)", "23", "qsv_h264"),
    EncoderConfig("hevc_qsv", "hevc_qsv (Intel)", "28", "qsv_h265"),
]

ENCODER_MAP = {e.display_name: e for e in ENCODERS}
ENCODER_TAG_MAP = {e.name: e.tag for e in ENCODERS}


# =======================
# 常量定义
# =======================
VIDEO_EXTS: tuple[str, ...] = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".mts", ".m2ts", ".rm", ".rmvb", ".mpg", ".mpeg", ".vob",
    ".3gp", ".f4v", ".asf", ".ogv", ".dv"
)
CONFIG_FILE = "config.json"
TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "webvtt"}

ENCODER_DISPLAY_MAP = {e.display_name: e.name for e in ENCODERS}
ENCODER_DEFAULT_CRF = {e.display_name: e.default_crf for e in ENCODERS}
ACTIVE_ENCODER_NAMES: list[str] = list(ENCODER_DISPLAY_MAP.keys())


def probe_available_encoder_names() -> set[str]:
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="ignore",
            creationflags=CREATE_NO_WINDOW, timeout=15
        )
    except Exception:
        return set()
    names: set[str] = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1]):
            names.add(parts[1])
    return names

# 跨平台兼容
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# =======================
# 缓存管理
# =======================
def load_cache() -> dict[str, VideoInfo]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        logger.error(f"加载缓存失败: {e}")
        return {}


def save_cache(cache: dict[str, VideoInfo]) -> None:
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, separators=(',', ':'))
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        os.rename(tmp, CONFIG_FILE)
    except Exception as e:
        logger.error(f"保存缓存失败: {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


# =======================
# ffprobe 工具函数
# =======================
def _run_ffprobe(cmd: list[str]) -> Optional[dict]:
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            creationflags=CREATE_NO_WINDOW,
            timeout=30
        )
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        logger.error(f"ffprobe 超时: {cmd}")
        return None
    except Exception as e:
        logger.debug(f"ffprobe 执行失败: {e}")
        return None


def probe_streams_detail(path: str) -> tuple[int, list[dict]]:
    data = _run_ffprobe([
        "ffprobe", "-v", "error",
        "-show_streams", "-of", "json", path
    ])
    if not data:
        return 1, []

    audio = 0
    sub_streams = []
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            audio += 1
        elif s.get("codec_type") == "subtitle":
            sub_streams.append(s)
    return audio, sub_streams


def probe_resolution(path: str) -> tuple[int, int]:
    data = _run_ffprobe([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", path
    ])
    try:
        streams = data.get("streams", [])
        s = streams[0] if streams else {}
        return int(s.get("width", 1920)), int(s.get("height", 1080))
    except Exception:
        return 1920, 1080


def probe_framerate(path: str) -> tuple[int, int]:
    data = _run_ffprobe([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "json", path
    ])
    try:
        streams = data.get("streams", [])
        s = streams[0] if streams else {}
        rfr = s.get("r_frame_rate", "30/1")
        num, den = rfr.split("/")
        return int(num), int(den)
    except Exception:
        return 30, 1


def probe_video_quality(path: str) -> tuple[str, int]:
    data = _run_ffprobe([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,bit_rate",
        "-of", "json", path
    ])
    try:
        streams = data.get("streams", [])
        s = streams[0] if streams else {}
        codec = s.get("codec_name", "unknown")
        br = s.get("bit_rate")
        bitrate_kbps = int(br) // 1000 if br and str(br).isdigit() else 0
        return codec, bitrate_kbps
    except Exception:
        return "unknown", 0


def get_video_duration(file_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", creationflags=CREATE_NO_WINDOW, timeout=30
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def probe_audio_sub_count(path: str) -> tuple[int, int]:
    audio_cnt, sub_streams = probe_streams_detail(path)
    return audio_cnt, len(sub_streams)


def detect_animation(path: str, seconds: int = 8) -> bool:
    try:
        cmd = [
            "ffmpeg", "-v", "error",
            "-t", str(seconds),
            "-i", path,
            "-vf", "signalstats",
            "-f", "null", "-"
        ]
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="ignore",
            creationflags=CREATE_NO_WINDOW, timeout=60
        )
        entropy_vals = []
        for line in p.stderr.splitlines():
            if "entropy" in line:
                m = re.search(r"entropy:\s*([0-9.]+)", line)
                if m:
                    entropy_vals.append(float(m.group(1)))
        if not entropy_vals:
            return False
        avg_entropy = sum(entropy_vals) / len(entropy_vals)
        return avg_entropy <= 2.5
    except subprocess.TimeoutExpired:
        logger.warning(f"动画检测超时: {path}")
        return False
    except Exception as e:
        logger.debug(f"动画检测失败: {e}")
        return False


def pick_ref_bframes(width: int, height: int) -> tuple[int, int]:
    pixels = width * height
    if pixels <= 1280 * 720:
        return 6, 8
    elif pixels <= 1920 * 1080:
        return 5, 8
    elif pixels <= 2560 * 1440:
        return 4, 6
    else:
        return 3, 4


def evaluate_compress_value(codec: str, bitrate_kbps: int, mb_per_min: float) -> tuple[int, int]:
    score = 0

    if codec in ("mpeg4", "xvid", "divx"):
        score += 40
    elif codec in ("h264", "avc"):
        score += 25
    elif codec in ("hevc", "h265", "av1", "vp9"):
        score -= 30

    if bitrate_kbps > 6000:
        score += 30
    elif bitrate_kbps > 3500:
        score += 15
    elif 0 < bitrate_kbps < 2500:
        score -= 20

    if mb_per_min > 80:
        score += 30
    elif mb_per_min > 50:
        score += 15
    elif mb_per_min < 40:
        score -= 20

    score = max(0, min(score, 100))

    if score >= 70:
        save_pct = 60
    elif score >= 50:
        save_pct = 40
    elif score >= 30:
        save_pct = 25
    else:
        save_pct = 10

    return score, save_pct


def analyze_video(path: str, cache: Optional[dict[str, VideoInfo]] = None) -> Optional[VideoInfo]:
    try:
        stat = os.stat(path)
    except Exception:
        return None

    size = stat.st_size
    mtime = stat.st_mtime

    if cache is not None:
        cached = cache.get(path)
        if cached and cached["size"] == size and cached["mtime"] == mtime:
            return cached

    duration = get_video_duration(path)
    if duration <= 0:
        return None

    size_mb = size / 1024 / 1024
    mb_per_min = size_mb / (duration / 60)
    audio_cnt, sub_cnt = probe_audio_sub_count(path)
    codec, bitrate_kbps = probe_video_quality(path)
    score, save_pct = evaluate_compress_value(codec, bitrate_kbps, mb_per_min)

    info: VideoInfo = {
        "name": os.path.basename(path),
        "path": path,
        "size": size,
        "mtime": mtime,
        "duration": duration,
        "size_mb": size_mb,
        "mb_per_min": mb_per_min,
        "audio_cnt": audio_cnt,
        "sub_cnt": sub_cnt,
        "codec": codec,
        "bitrate_kbps": bitrate_kbps,
        "compress_score": score,
        "save_pct": save_pct
    }

    if cache is not None:
        cache[path] = info

    return info


# =======================
# 扫描线程
# =======================
class ScanThread(QThread):
    video_found = pyqtSignal(object)
    scan_finished = pyqtSignal()

    def __init__(self, folder: str, workers: int = 4):
        super().__init__()
        self.folder = folder
        self.cache = load_cache()
        self._stop = False
        self.workers = workers

    def stop(self) -> None:
        self._stop = True

    def _collect_files(self) -> list[str]:
        file_list = []
        for root, _, files in os.walk(self.folder):
            if self._stop:
                break
            for name in files:
                if self._stop:
                    break
                if name.lower().endswith(VIDEO_EXTS):
                    file_list.append(os.path.abspath(os.path.join(root, name)))
        return file_list

    def _analyze_one(self, path: str) -> Optional[VideoInfo]:
        return analyze_video(path, self.cache)

    def run(self) -> None:
        file_list = self._collect_files()
        total = len(file_list)
        if total == 0:
            self.scan_finished.emit()
            return

        count = 0
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = {pool.submit(self._analyze_one, p): p for p in file_list}
            for future in as_completed(futures):
                if self._stop:
                    break
                try:
                    info = future.result()
                    if info:
                        self.video_found.emit(info)
                        count += 1
                        if count % 100 == 0:
                            save_cache(self.cache)
                except Exception:
                    pass
        finally:
            pool.shutdown(wait=False)

        if not self._stop:
            save_cache(self.cache)
        self.scan_finished.emit()

# =======================
# 保存列表线程
# =======================
class SaveListThread(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, data: dict[str, VideoInfo], path: str):
        super().__init__()
        self.data = data
        self.path = path

    def run(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            self.finished.emit(self.path)
        except Exception as e:
            self.error.emit(str(e))


# =======================
# 加载列表线程
# =======================
class LoadListThread(QThread):
    video_found = pyqtSignal(object)
    finished = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(self, file_path: str, existing: set[str]):
        super().__init__()
        self.file_path = file_path
        self.existing = existing
        self.cache = load_cache()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.error.emit(str(e))
            return

        if isinstance(data, dict):
            paths = [k for k, v in data.items() if isinstance(v, dict)]
        elif isinstance(data, list):
            paths = data
        else:
            self.error.emit("文件格式错误")
            return

        count = 0
        total = len(paths)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for file_path in paths:
                if self._stop:
                    break
                if file_path in self.existing:
                    continue
                if not os.path.exists(file_path):
                    continue
                futures[pool.submit(analyze_video, file_path, self.cache)] = file_path

            for future in as_completed(futures):
                if self._stop:
                    break
                try:
                    info = future.result()
                    if info:
                        self.video_found.emit(info)
                        count += 1
                except Exception:
                    pass

        save_cache(self.cache)
        self.finished.emit(count)


# =======================
# 日志对话框
# =======================
class ConvertLogDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("后台正在转换...")
        self.resize(550, 400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.label_file = QLabel("当前视频进度: 0%")
        self.progress_file = QProgressBar()
        self.progress_file.setFormat("当前视频: %p%")

        self.label_total = QLabel("总体进度: 0%")
        self.progress_total = QProgressBar()
        self.progress_total.setFormat("总体进度: %p%")

        self.text_log = QTextEdit()
        self.text_log.setReadOnly(True)

        layout.addWidget(self.label_file)
        layout.addWidget(self.progress_file)
        layout.addWidget(self.label_total)
        layout.addWidget(self.progress_total)
        layout.addWidget(QLabel("后台日志:"))
        layout.addWidget(self.text_log)

    def append_log(self, msg: str) -> None:
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.text_log.append(f"[{timestamp}] {msg}")

    def update_progress(self, file_pct: int, total_pct: int) -> None:
        self.progress_file.setValue(file_pct)
        self.progress_total.setValue(total_pct)
        self.label_file.setText(f"当前视频进度: {file_pct}%")
        self.label_total.setText(f"总体进度: {total_pct}%")


# =======================
# 压缩线程
# =======================
class CompressThread(QThread):
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    finished = pyqtSignal()
    output_ready = pyqtSignal(str, str)

    def __init__(self, files: list[str], delete_source: bool = False, encoder: str = "libx264", crf: int = 21, output_format: str = ".mkv", two_pass: bool = False, output_dir: str = ""):
        super().__init__()
        self.files = files
        self.delete_source = delete_source
        self.encoder = encoder
        self.crf = crf
        self.output_format = output_format
        self.two_pass = two_pass
        self.output_dir = output_dir
        self._pause = False
        self._stop = False
        self._process: Optional[subprocess.Popen] = None
        self._current_output: Optional[str] = None
        self._bitrate = 0

    def pause(self) -> None:
        if self._process and not self._pause:
            try:
                psutil.Process(self._process.pid).suspend()
                self._pause = True
            except psutil.NoSuchProcess:
                pass

    def resume(self) -> None:
        if self._process and self._pause:
            try:
                psutil.Process(self._process.pid).resume()
                self._pause = False
            except psutil.NoSuchProcess:
                pass

    def stop(self) -> None:
        self._stop = True
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
            except Exception:
                pass

    def _build_x264_cmd(self, src: str, dst: str, is_animation: bool, width: int, height: int) -> list[str]:
        ref, bframes = pick_ref_bframes(width, height)
        tune_hint = "animation" if is_animation else "film"
        fps_num, fps_den = probe_framerate(src)
        keyint = max(int(fps_num * 8 / fps_den), 1)
        min_keyint = max(int(fps_num / fps_den), 1)
        x264_params = (
            f"ref={ref}:"
            f"bframes={bframes}:b-adapt=2:"
            "me=umh:merange=34:subme=10:"
            f"keyint={keyint}:min-keyint={min_keyint}:scenecut=60:"
            "rc-lookahead=240:"
            "trellis=2:"
            "aq-mode=3:aq-strength=0.9:"
            "psy-rd=1.0\\:0.15:"
            "deblock=-1\\:-1:"
            "qcomp=0.5:"
            "partitions=all:no-fast-pskip:"
            "direct=auto"
        )
        return [
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", str(self.crf),
            "-tune", tune_hint,
            "-x264-params", x264_params,
        ]

    def _build_encoder_args(self, src: str, dst: str, is_animation: bool, width: int, height: int) -> list[str]:
        if self.encoder == "libx264":
            return self._build_x264_cmd(src, dst, is_animation, width, height)
        elif self.encoder == "libx265":
            args = ["-c:v", "libx265", "-preset", "slow", "-crf", str(self.crf)]
            if is_animation:
                args += ["-tune", "animation"]
            x265_params = "log-level=error:ref=4:bframes=8:b-adapt=2:rd=4:psy-rd=2.0:aq-mode=3:aq-strength=0.8:deblock=-1,-1"
            args += ["-x265-params", x265_params]
            return args
        elif self.encoder == "libvpx-vp9":
            return [
                "-c:v", "libvpx-vp9",
                "-crf", str(self.crf),
                "-b:v", "0",
                "-deadline", "good",
                "-cpu-used", "2",
                "-row-mt", "1",
            ]
        elif self.encoder == "libaom-av1":
            return [
                "-c:v", "libaom-av1",
                "-crf", str(self.crf),
                "-b:v", "0",
                "-cpu-used", "6",
                "-row-mt", "1",
                "-tiles", "2x2",
                "-strict", "-2",
            ]
        elif self.encoder == "h264_nvenc":
            return [
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-rc", "constqp",
                "-qp", str(self.crf),
                "-b:v", "0",
            ]
        elif self.encoder == "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc",
                "-preset", "p4",
                "-rc", "constqp",
                "-qp", str(self.crf),
                "-b:v", "0",
            ]
        elif self.encoder == "h264_qsv":
            return [
                "-c:v", "h264_qsv",
                "-preset", "medium",
                "-global_quality", str(self.crf),
                "-b:v", "0",
            ]
        elif self.encoder == "hevc_qsv":
            return [
                "-c:v", "hevc_qsv",
                "-preset", "medium",
                "-global_quality", str(self.crf),
                "-b:v", "0",
            ]
        else:
            return ["-c:v", self.encoder, "-crf", str(self.crf)]

    def _build_encoder_args_pass1(self, src: str, dst: str, is_animation: bool, width: int, height: int, passlog: str) -> list[str]:
        if self.encoder == "libx264":
            ref, bframes = pick_ref_bframes(width, height)
            tune_hint = "animation" if is_animation else "film"
            fps_num, fps_den = probe_framerate(src)
            keyint = max(int(fps_num * 8 / fps_den), 1)
            min_keyint = max(int(fps_num / fps_den), 1)
            x264_params = (
                f"ref={ref}:"
                f"bframes={bframes}:b-adapt=2:"
                "me=umh:merange=34:subme=10:"
                f"keyint={keyint}:min-keyint={min_keyint}:scenecut=60:"
                "rc-lookahead=240:"
                "trellis=2:"
                "aq-mode=3:aq-strength=0.9:"
                "psy-rd=1.0\\:0.15:"
                "deblock=-1\\:-1:"
                "qcomp=0.5:"
                "partitions=all:no-fast-pskip:"
                "direct=auto"
            )
            rate_args = self._rate_control_args()
            return [
                "-c:v", "libx264",
                "-preset", "slow",
                *rate_args,
                "-tune", tune_hint,
                "-x264-params", x264_params,
                "-pass", "1", "-passlogfile", passlog,
            ]
        elif self.encoder == "libx265":
            x265_params = f"pass=1:stats={passlog}:ref=4:bframes=8:b-adapt=2:rd=4:psy-rd=2.0:aq-mode=3:aq-strength=0.8"
            return [
                "-c:v", "libx265",
                "-preset", "slow",
                *self._rate_control_args(),
                "-x265-params", x265_params,
            ]
        return []

    def _build_encoder_args_pass2(self, src: str, dst: str, is_animation: bool, width: int, height: int, passlog: str) -> list[str]:
        if self.encoder == "libx264":
            ref, bframes = pick_ref_bframes(width, height)
            tune_hint = "animation" if is_animation else "film"
            fps_num, fps_den = probe_framerate(src)
            keyint = max(int(fps_num * 8 / fps_den), 1)
            min_keyint = max(int(fps_num / fps_den), 1)
            x264_params = (
                f"ref={ref}:"
                f"bframes={bframes}:b-adapt=2:"
                "me=umh:merange=34:subme=10:"
                f"keyint={keyint}:min-keyint={min_keyint}:scenecut=60:"
                "rc-lookahead=240:"
                "trellis=2:"
                "aq-mode=3:aq-strength=0.9:"
                "psy-rd=1.0\\:0.15:"
                "deblock=-1\\:-1:"
                "qcomp=0.5:"
                "partitions=all:no-fast-pskip:"
                "direct=auto"
            )
            rate_args = self._rate_control_args()
            return [
                "-c:v", "libx264",
                "-preset", "slow",
                *rate_args,
                "-tune", tune_hint,
                "-x264-params", x264_params,
                "-pass", "2", "-passlogfile", passlog,
            ]
        elif self.encoder == "libx265":
            x265_params = f"pass=2:stats={passlog}:ref=4:bframes=8:b-adapt=2:rd=4:psy-rd=2.0:aq-mode=3:aq-strength=0.8"
            return [
                "-c:v", "libx265",
                "-preset", "slow",
                *self._rate_control_args(),
                "-x265-params", x265_params,
            ]
        return []

    def _rate_control_args(self) -> list[str]:
        if self._bitrate:
            return ["-b:v", f"{self._bitrate}k"]
        return ["-crf", str(self.crf)]

    def run(self) -> None:
        total = len(self.files)
        enc_tag = ENCODER_TAG_MAP.get(self.encoder, self.encoder)

        for idx, src in enumerate(self.files, start=1):
            if self._stop:
                break

            duration_src = get_video_duration(src)
            if duration_src <= 0:
                self.log.emit(f"无法获取时长: {os.path.basename(src)}")
                continue

            duration_us = duration_src * 1_000_000
            base, _ = os.path.splitext(src)
            base_name = os.path.basename(base)
            if self.output_dir:
                dst = os.path.join(self.output_dir, f"{base_name}_{enc_tag}{self.output_format}")
            else:
                dst = f"{base}_{enc_tag}{self.output_format}"
            self._current_output = dst

            width, height = probe_resolution(src)
            is_animation = detect_animation(src)

            self._bitrate = 0
            if self.two_pass and self.encoder in ("libx264", "libx265"):
                _, src_kbps = probe_video_quality(src)
                if src_kbps > 0:
                    self._bitrate = max(800, int(src_kbps * 0.6))

            self.log.emit(
                f"参数: {width}x{height} | "
                f"{'动画' if is_animation else '实拍'} | "
                f"encoder={self.encoder} | crf={self.crf}"
                + (f" | 2pass目标码率={self._bitrate}kbps" if self._bitrate else "")
            )

            if self.two_pass and self.encoder in ("libx264", "libx265"):
                # 两遍编码
                log_file = f"{base}_ffmpeg2pass.log"
                passlog_file = f"{base}_ffmpeg2pass.log"
                
                # 第一遍
                self.log.emit(f"两遍编码 - 第一遍: {os.path.basename(src)}")
                cmd_pass1 = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", src,
                    "-map", "v:0", "-map", "a?", "-map", "s?",
                ]
                cmd_pass1 += self._build_encoder_args_pass1(src, dst, is_animation, width, height, passlog_file)
                cmd_pass1 += ["-an", "-f", "null", "-"]
                
                try:
                    p1 = subprocess.Popen(
                        cmd_pass1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.PIPE, encoding="utf-8", errors="ignore",
                        creationflags=CREATE_NO_WINDOW
                    )
                    self._process = p1
                    p1.wait()
                    if p1.returncode != 0:
                        self.log.emit(f"第一遍编码失败: {os.path.basename(src)}")
                        continue
                except Exception as e:
                    self.log.emit(f"第一遍编码出错: {e}")
                    continue
                
                # 第二遍
                self.log.emit(f"两遍编码 - 第二遍: {os.path.basename(src)}")
                cmd_pass2 = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", src,
                    "-map", "v:0", "-map", "a?", "-map", "s?",
                ]
                cmd_pass2 += self._build_encoder_args_pass2(src, dst, is_animation, width, height, passlog_file)
                cmd_pass2 += [
                    "-c:a", "copy", "-c:s", "copy",
                    "-map_metadata", "0", "-map_chapters", "0",
                    "-progress", "pipe:1", "-nostats",
                    dst,
                ]
                cmd = cmd_pass2
            else:
                # 单遍编码
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", src,
                    "-map", "v:0", "-map", "a?", "-map", "s?",
                ]
                cmd += self._build_encoder_args(src, dst, is_animation, width, height)
                cmd += [
                    "-c:a", "copy", "-c:s", "copy",
                    "-map_metadata", "0", "-map_chapters", "0",
                    "-progress", "pipe:1", "-nostats",
                    dst,
                ]

            self.log.emit(f"开始压缩: {os.path.basename(src)}")

            try:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE, encoding="utf-8", errors="ignore",
                    creationflags=CREATE_NO_WINDOW
                )
            except Exception as e:
                self.log.emit(f"启动 ffmpeg 失败: {e}")
                continue

            self._process = p

            try:
                for line in p.stdout:
                    if self._stop:
                        p.terminate()
                        break
                    if not line.startswith("out_time_ms="):
                        continue
                    value = line.split("=", 1)[1].strip()
                    if not value.isdigit():
                        continue
                    out_ms = int(value)
                    percent = max(0, min(int(out_ms / duration_us * 100), 100))
                    total_percent = int(((idx - 1) + percent / 100) / total * 100)
                    self.progress.emit(percent, total_percent)
            finally:
                if p.stdout:
                    p.stdout.close()

            p.wait()

            if p.returncode != 0:
                self.log.emit(f"ffmpeg 编码失败: {os.path.basename(src)}")
                continue

            if not os.path.exists(dst) or os.path.getsize(dst) == 0:
                self.log.emit("输出文件为空，压缩失败")
                continue

            # 清理两遍编码的临时文件
            if self.two_pass:
                for tmp in [f"{base}_ffmpeg2pass.log", f"{base}_ffmpeg2pass.log.mbtree"]:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass

            self.output_ready.emit(src, dst)

            if not self._stop and self.delete_source:
                try:
                    os.remove(src)
                    self.log.emit(f"已删除源文件: {os.path.basename(src)}")
                except Exception as e:
                    self.log.emit(f"删除源文件失败: {e}")

        if self._stop and self._current_output:
            try:
                if os.path.exists(self._current_output):
                    os.remove(self._current_output)
                    self.log.emit(f"已删除未完成文件: {os.path.basename(self._current_output)}")
            except Exception:
                self.log.emit(f"无法删除残留文件: {os.path.basename(self._current_output)}")

        self.finished.emit()


# =======================
# GUI 主窗口
# =======================
class VideoScanner(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("视频扫描 & 一键压缩")
        self.resize(1100, 600)
        self.setMinimumSize(900, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题
        title_label = QLabel("视频扫描 & 一键压缩")
        title_label.setObjectName("title")
        layout.addWidget(title_label)

        # 启用拖拽
        self.setAcceptDrops(True)
        self.drag_label = QLabel("  拖拽视频文件到此处即可导入  ")
        self.drag_label.setObjectName("dragLabel")
        self.drag_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drag_label.setStyleSheet("border: 2px dashed #bdc3c7; border-radius: 8px; padding: 8px; color: #7f8c8d; font-size: 12px;")
        layout.addWidget(self.drag_label)

        # 第一行：选项
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addStretch()
        layout.addLayout(row1)

        # 第二行：压缩设置
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(QLabel("编码器:"))
        self.combo_encoder = QComboBox()
        self.combo_encoder.addItems(ACTIVE_ENCODER_NAMES)
        self.combo_encoder.setMinimumWidth(180)
        row2.addWidget(self.combo_encoder)
        row2.addWidget(QLabel("CRF:"))
        self.lineEdit_crf = QLineEdit()
        self.lineEdit_crf.setText(ENCODER_DEFAULT_CRF.get(self.combo_encoder.currentText(), "21"))
        self.lineEdit_crf.setMaximumWidth(50)
        row2.addWidget(self.lineEdit_crf)
        row2.addWidget(QLabel("输出格式:"))
        self.combo_format = QComboBox()
        self.combo_format.addItems([".mp4", ".mkv"])
        self.combo_format.setMinimumWidth(80)
        row2.addWidget(self.combo_format)
        self.chk_two_pass = QCheckBox("两遍编码")
        self.chk_auto_shutdown = QCheckBox("压缩后自动关机")
        row2.addSpacing(20)
        row2.addWidget(self.chk_two_pass)
        row2.addWidget(self.chk_auto_shutdown)
        row2.addSpacing(20)
        self.chk_custom_output = QCheckBox("指定输出目录")
        self.chk_custom_output.stateChanged.connect(self._toggle_output_dir)
        self.btn_output_dir = QPushButton("选择目录")
        self.btn_output_dir.setEnabled(False)
        self.btn_output_dir.setMaximumWidth(100)
        self.output_dir = ""
        row2.addWidget(self.chk_custom_output)
        row2.addWidget(self.btn_output_dir)
        row2.addStretch()
        layout.addLayout(row2)

        # 第三行：文件操作
        row3a = QHBoxLayout()
        row3a.setSpacing(6)
        self.btn_import = QPushButton("导入文件")
        self.btn_scan = QPushButton("扫描文件夹")
        self.btn_stop_scan = QPushButton("停止扫描")
        self.btn_save_list = QPushButton("保存列表")
        self.btn_load_list = QPushButton("加载列表")
        row3a.addWidget(self.btn_import)
        row3a.addWidget(self.btn_scan)
        row3a.addWidget(self.btn_stop_scan)
        row3a.addWidget(self.btn_save_list)
        row3a.addWidget(self.btn_load_list)
        row3a.addStretch()
        layout.addLayout(row3a)

        # 第四行：压缩控制
        row3b = QHBoxLayout()
        row3b.setSpacing(6)
        self.btn_compress = QPushButton("压缩选中")
        self.btn_pause = QPushButton("暂停")
        self.btn_resume = QPushButton("继续")
        self.btn_stop = QPushButton("停止")
        self.combo_select_by = QComboBox()
        self.combo_select_by.addItems(["全部", "1星", "2星", "3星", "4星", "5星"])
        self.combo_select_by.currentIndexChanged.connect(self.select_by_stars)
        self.chk_delete_source = QCheckBox("压缩后删除源文件")
        row3b.addWidget(self.btn_compress)
        row3b.addWidget(self.btn_pause)
        row3b.addWidget(self.btn_resume)
        row3b.addWidget(self.btn_stop)
        row3b.addSpacing(20)
        row3b.addWidget(QLabel("按价值勾选:"))
        row3b.addWidget(self.combo_select_by)
        row3b.addSpacing(10)
        row3b.addWidget(self.chk_delete_source)
        row3b.addStretch()
        layout.addLayout(row3b)

        self.btn_stop_scan.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(False)

        self.btn_stop_scan.setObjectName("btnStopScan")
        self.btn_compress.setObjectName("btnCompress")
        self.btn_pause.setObjectName("btnPause")
        self.btn_resume.setObjectName("btnResume")
        self.btn_stop.setObjectName("btnStop")

        # 表格
        self.table = QTableWidget(0, 12)
        self.table.setObjectName("mainTable")
        self.table.setHorizontalHeaderLabels([
            "✔", "文件名", "大小(MB)", "时长(分)", "MB/分",
            "音轨", "字幕", "编码", "压缩价值", "预计节省", "路径", "输出文件"
        ])
        for col, width in [(0, 40), (1, 200), (5, 50), (6, 50), (7, 60), (8, 80), (9, 80), (10, 300), (11, 300)]:
            self.table.setColumnWidth(col, width)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # 选中统计
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("statusLabel")
        layout.addWidget(self.stats_label)

        # 进度条
        self.progress_file = QProgressBar()
        self.progress_file.setFormat("当前视频: %p%")
        self.progress_total = QProgressBar()
        self.progress_total.setFormat("总体进度: %p%")
        layout.addWidget(self.progress_file)
        layout.addWidget(self.progress_total)

        # 状态栏
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.status_label)

        # 信号绑定
        self.btn_import.clicked.connect(self.import_files)
        self.btn_scan.clicked.connect(self.select_folder)
        self.btn_stop_scan.clicked.connect(self.stop_scan)
        self.btn_save_list.clicked.connect(self.save_list)
        self.btn_load_list.clicked.connect(self.load_list)
        self.btn_output_dir.clicked.connect(self._select_output_dir)
        self.btn_compress.clicked.connect(self.compress_checked)
        self.btn_pause.clicked.connect(self.pause_compress)
        self.btn_resume.clicked.connect(self.resume_compress)
        self.btn_stop.clicked.connect(self.stop_compress)
        self.combo_encoder.currentTextChanged.connect(self.on_encoder_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self._suppress_stats = True
        self.table.itemChanged.connect(self._on_table_changed)

        self.thread: Optional[ScanThread] = None
        self.compress_thread: Optional[CompressThread] = None
        self.log_dialog: Optional[ConvertLogDialog] = None
        self.load_history()
        self._suppress_stats = False
        self._refresh_stats()

    def on_encoder_changed(self, text: str) -> None:
        self.lineEdit_crf.setText(ENCODER_DEFAULT_CRF.get(text, "21"))
        encoder = ENCODER_DISPLAY_MAP.get(text, "")
        supports_two_pass = encoder in ("libx264", "libx265")
        if supports_two_pass:
            self.chk_two_pass.setEnabled(True)
        else:
            self.chk_two_pass.setChecked(False)
            self.chk_two_pass.setEnabled(False)

    def _toggle_output_dir(self, state: int) -> None:
        self.btn_output_dir.setEnabled(state == 2)
        if state != 2:
            self.output_dir = ""

    def _select_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.output_dir = d
            self.btn_output_dir.setText(os.path.basename(d))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drag_label.setStyleSheet("border: 2px dashed #3498db; border-radius: 8px; padding: 8px; color: #3498db; font-size: 12px; background-color: #ebf5fb;")

    def dragLeaveEvent(self, event):
        self.drag_label.setStyleSheet("border: 2px dashed #bdc3c7; border-radius: 8px; padding: 8px; color: #7f8c8d; font-size: 12px;")

    def dropEvent(self, event):
        self.drag_label.setStyleSheet("border: 2px dashed #bdc3c7; border-radius: 8px; padding: 8px; color: #7f8c8d; font-size: 12px;")
        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path) and path.lower().endswith(VIDEO_EXTS):
                files.append(path)
            elif os.path.isdir(path):
                for root, _, fnames in os.walk(path):
                    for fname in fnames:
                        if fname.lower().endswith(VIDEO_EXTS):
                            files.append(os.path.abspath(os.path.join(root, fname)))
        if files:
            self.status_label.setText(f"拖入 {len(files)} 个文件，正在分析...")
            cache = load_cache()
            count = 0
            self._suppress_stats = True
            try:
                for path in files:
                    info = analyze_video(path, cache)
                    if info:
                        self.add_video(info)
                        count += 1
            finally:
                self._suppress_stats = False
            save_cache(cache)
            self._refresh_stats()
            self.status_label.setText(f"导入完成 - 成功导入 {count} 个文件")

    def select_by_stars(self, index: int) -> None:
        self._suppress_stats = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if not item:
                    continue
                if index == 0:
                    item.setCheckState(Qt.CheckState.Checked)
                    continue
                score_item = self.table.item(row, 8)
                if not score_item:
                    item.setCheckState(Qt.CheckState.Unchecked)
                    continue
                score = score_item.data(Qt.ItemDataRole.UserRole) or 0
                if index == 1:
                    checked = score > 0
                elif index == 2:
                    checked = score >= 30
                elif index == 3:
                    checked = score >= 50
                else:
                    checked = score >= 70
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        finally:
            self._suppress_stats = False
        self._refresh_stats()

    def _on_table_changed(self, item) -> None:
        if not self._suppress_stats and item.column() == 0:
            self._refresh_stats()

    def _refresh_stats(self) -> None:
        total = 0
        checked = 0
        checked_mb = 0.0
        est_mb = 0.0
        for row in range(self.table.rowCount()):
            check_item = self.table.item(row, 0)
            if not check_item:
                continue
            total += 1
            try:
                size_mb = float(self.table.item(row, 2).text())
            except Exception:
                size_mb = 0.0
            save_item = self.table.item(row, 9)
            save_pct = 0
            if save_item:
                m = re.search(r"(\d+)", save_item.text())
                if m:
                    save_pct = int(m.group(1))
            if check_item.checkState() == Qt.CheckState.Checked:
                checked += 1
                checked_mb += size_mb
                est_mb += size_mb * save_pct / 100.0
        if total == 0:
            self.stats_label.setText("暂无视频")
            return
        checked_gb = checked_mb / 1024.0
        est_gb = est_mb / 1024.0
        self.stats_label.setText(
            f"共 {total} 个视频 · 已勾选 {checked} 个 · 选中体积 {checked_gb:.2f} GB · "
            f"预计可释放约 {est_gb:.2f} GB"
        )

    def stop_scan(self) -> None:
        if self.thread:
            self.thread.stop()
            self.btn_stop_scan.setEnabled(False)

    def pause_compress(self) -> None:
        if self.compress_thread:
            self.compress_thread.pause()
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(True)

    def resume_compress(self) -> None:
        if self.compress_thread:
            self.compress_thread.resume()
            self.btn_resume.setEnabled(False)
            self.btn_pause.setEnabled(True)

    def stop_compress(self) -> None:
        if self.compress_thread:
            reply = QMessageBox.question(
                self, "确认停止", "确定要停止当前压缩任务？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.compress_thread.stop()

    def compress_checked(self) -> None:
        files = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.checkState() == Qt.CheckState.Checked:
                files.append(self.table.item(row, 10).text())

        if not files:
            QMessageBox.warning(self, "提示", "未选择任何视频")
            return

        selected_text = self.combo_encoder.currentText()
        encoder = ENCODER_DISPLAY_MAP.get(selected_text, "libx264")
        two_pass = self.chk_two_pass.isChecked()

        if two_pass and encoder not in ("libx264", "libx265"):
            QMessageBox.warning(self, "提示", "两遍编码仅支持 libx264 和 libx265 编码器")
            return

        try:
            crf = int(self.lineEdit_crf.text().strip())
        except ValueError:
            crf = int(ENCODER_DEFAULT_CRF.get(selected_text, "23"))
            QMessageBox.warning(self, "提示", f"CRF/质量值无效，已使用默认值 {crf}")
        max_crf = 63 if encoder in ("libvpx-vp9", "libaom-av1") else 51
        crf = max(0, min(crf, max_crf))
        output_format = self.combo_format.currentText()

        self.btn_compress.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.btn_stop_scan.setEnabled(False)

        self.compress_thread = CompressThread(
            files,
            delete_source=self.chk_delete_source.isChecked(),
            encoder=encoder,
            crf=crf,
            output_format=output_format,
            two_pass=two_pass,
            output_dir=self.output_dir
        )
        self.compress_thread.finished.connect(self.compress_done)
        self.compress_thread.progress.connect(self.update_progress)
        self.compress_thread.output_ready.connect(self.update_output_path)

        self.log_dialog = ConvertLogDialog(self)
        self.compress_thread.log.connect(self.log_dialog.append_log)
        self.compress_thread.progress.connect(self.log_dialog.update_progress)
        self.compress_thread.finished.connect(self.log_dialog.accept)
        self.log_dialog.show()

        self.compress_thread.start()
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.status_label.setText(f"正在压缩 {len(files)} 个视频...")

    def compress_done(self) -> None:
        self.btn_compress.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_import.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(False)
        if self.log_dialog and self.log_dialog.isVisible():
            self.log_dialog.accept()
        self.status_label.setText("压缩任务完成")
        
        if self.chk_auto_shutdown.isChecked():
            reply = QMessageBox.question(
                self, "自动关机",
                "压缩已完成，是否立即关机？\n（30秒后将自动关机，取消可阻止关机）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.status_label.setText("正在关机...")
                os.system("shutdown /s /t 30 /c \"视频压缩完成后自动关机\"")
            else:
                os.system("shutdown /a")
        else:
            QMessageBox.information(self, "完成", "压缩任务完成")

    def update_output_path(self, src_path: str, dst_path: str) -> None:
        for row in range(self.table.rowCount()):
            path_item = self.table.item(row, 10)
            if path_item and path_item.text() == src_path:
                self.table.setItem(row, 11, QTableWidgetItem(dst_path))
                break

    def add_video(self, v: VideoInfo) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 10).text() == v["path"]:
                return

        row = self.table.rowCount()
        self.table.insertRow(row)

        check_item = QTableWidgetItem()
        check_item.setFlags(check_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        check_item.setCheckState(Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, check_item)
        self.table.setItem(row, 1, QTableWidgetItem(v["name"]))
        self.table.setItem(row, 2, QTableWidgetItem(f"{v['size_mb']:.2f}"))
        self.table.setItem(row, 3, QTableWidgetItem(f"{v['duration'] / 60:.1f}"))
        self.table.setItem(row, 4, QTableWidgetItem(f"{v['mb_per_min']:.2f}"))
        self.table.setItem(row, 5, QTableWidgetItem(str(v.get("audio_cnt", 0))))
        self.table.setItem(row, 6, QTableWidgetItem(str(v.get("sub_cnt", 0))))

        codec = v.get("codec", "unknown")
        score = v.get("compress_score", 0)
        save_pct = v.get("save_pct", 0)
        
        if score >= 70:
            color = "#27ae60"
            stars = "★★★★★"
        elif score >= 50:
            color = "#f39c12"
            stars = "★★★★☆"
        elif score >= 30:
            color = "#e67e22"
            stars = "★★★☆☆"
        elif score > 0:
            color = "#e74c3c"
            stars = "★★☆☆☆"
        else:
            color = "#95a5a6"
            stars = "☆☆☆☆☆"
        
        codec_item = QTableWidgetItem(codec)
        self.table.setItem(row, 7, codec_item)
        
        score_item = QTableWidgetItem(stars)
        score_item.setForeground(QColor(color))
        score_item.setData(Qt.ItemDataRole.UserRole, score)
        self.table.setItem(row, 8, score_item)
        
        save_item = QTableWidgetItem(f"~{save_pct}%")
        save_item.setForeground(QColor(color))
        self.table.setItem(row, 9, save_item)
        
        self.table.setItem(row, 10, QTableWidgetItem(v["path"]))
        self.table.setItem(row, 11, QTableWidgetItem(""))
        
        self.status_label.setText(f"已加载: {v['name']}")

    def show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        delete_action = menu.addAction("从列表中删除")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == delete_action:
            self.delete_selected_rows()

    def import_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择视频文件", "",
            "视频文件 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.mts *.m2ts *.rm *.rmvb *.mpg *.mpeg *.vob *.3gp *.f4v *.asf *.ogv *.dv)"
        )
        if not files:
            return
        self.btn_scan.setEnabled(False)
        self.status_label.setText(f"正在导入 {len(files)} 个文件...")
        cache = load_cache()
        count = 0
        self._suppress_stats = True
        try:
            for path in files:
                info = analyze_video(path, cache)
                if info:
                    self.add_video(info)
                    count += 1
        finally:
            self._suppress_stats = False
        save_cache(cache)
        self.btn_scan.setEnabled(True)
        self._refresh_stats()
        self.status_label.setText(f"导入完成 - 成功导入 {count} 个文件")

    def save_list(self) -> None:
        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "提示", "列表为空，无需保存")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存列表", "video_list.json",
            "JSON 文件 (*.json);;所有文件 (*)"
        )
        if not path:
            return
        cache = load_cache()
        data: dict[str, VideoInfo] = {}
        for row in range(self.table.rowCount()):
            path_item = self.table.item(row, 10)
            if path_item and path_item.text() in cache:
                data[path_item.text()] = cache[path_item.text()]
        self.status_label.setText("正在保存列表...")
        self._save_thread = SaveListThread(data, path)
        self._save_thread.finished.connect(lambda p: self.status_label.setText(f"列表已保存: {p}"))
        self._save_thread.error.connect(lambda e: QMessageBox.critical(self, "错误", f"保存失败: {e}"))
        self._save_thread.start()

    def load_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "加载列表", "",
            "JSON 文件 (*.json);;所有文件 (*)"
        )
        if not path:
            return
        existing = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 10)
            if item:
                existing.add(item.text())
        self.btn_load_list.setEnabled(False)
        self.status_label.setText("正在加载列表...")
        self._suppress_stats = True
        self._load_thread = LoadListThread(path, existing)
        self._load_thread.video_found.connect(self.add_video)
        self._load_thread.finished.connect(self._load_list_done)
        self._load_thread.error.connect(self._load_list_error)
        self._load_thread.start()

    def _load_list_done(self, count: int) -> None:
        self.btn_load_list.setEnabled(True)
        self._suppress_stats = False
        self._refresh_stats()
        self.status_label.setText(f"加载完成 - 新增 {count} 个文件")

    def _load_list_error(self, msg: str) -> None:
        self.btn_load_list.setEnabled(True)
        self._suppress_stats = False
        self._refresh_stats()
        QMessageBox.critical(self, "错误", f"加载失败: {msg}")

    def update_progress(self, file_percent: int, total_percent: int) -> None:
        self.progress_file.setValue(file_percent)
        self.progress_total.setValue(total_percent)

    def delete_selected_rows(self) -> None:
        rows = sorted(set(item.row() for item in self.table.selectedItems()), reverse=True)
        if not rows:
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定从列表中删除选中的 {len(rows)} 条记录？\n（不会删除视频文件）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        cache = load_cache()
        changed = False
        for row in rows:
            path_item = self.table.item(row, 10)
            if not path_item:
                continue
            path = path_item.text()
            if path in cache:
                del cache[path]
                changed = True
            self.table.removeRow(row)

        if changed:
            save_cache(cache)
        self._refresh_stats()

    def load_history(self) -> None:
        for v in load_cache().values():
            self.add_video(v)

    def select_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择视频目录")
        if not folder:
            return

        self.btn_scan.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.btn_stop_scan.setEnabled(True)
        self.btn_compress.setEnabled(False)
        self.status_label.setText(f"正在扫描: {folder}...")

        self._suppress_stats = True
        self.thread = ScanThread(folder)
        self.thread.video_found.connect(self.add_video)
        self.thread.scan_finished.connect(self.scan_done)
        self.thread.start()

    def scan_done(self) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_import.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)
        self.btn_compress.setEnabled(True)
        self._suppress_stats = False
        self._refresh_stats()
        self.status_label.setText(f"扫描完成 - 共 {self.table.rowCount()} 个视频文件")


# =======================
# 入口
# =======================
def main():
    app = QApplication(sys.argv)
    
    app.setStyleSheet("""
        QWidget {
            font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
            font-size: 13px;
        }

        QLabel#title {
            font-size: 16px;
            font-weight: bold;
            color: #1e293b;
            padding: 6px 0;
        }

        QLabel#statusLabel {
            color: #94a3b8;
            font-size: 12px;
        }

        QPushButton {
            background-color: #3b82f6;
            color: white;
            border: none;
            padding: 6px 14px;
            border-radius: 4px;
            font-weight: bold;
            min-width: 70px;
        }
        QPushButton:hover { background-color: #2563eb; }
        QPushButton:pressed { background-color: #1d4ed8; }
        QPushButton:disabled { background-color: #cbd5e1; color: #94a3b8; }

        QPushButton#btnCompress { background-color: #22c55e; }
        QPushButton#btnCompress:hover { background-color: #16a34a; }
        QPushButton#btnStop { background-color: #ef4444; }
        QPushButton#btnStop:hover { background-color: #dc2626; }
        QPushButton#btnPause { background-color: #f59e0b; }
        QPushButton#btnPause:hover { background-color: #d97706; }
        QPushButton#btnResume { background-color: #22c55e; }
        QPushButton#btnResume:hover { background-color: #16a34a; }
        QPushButton#btnStopScan { background-color: #ef4444; }
        QPushButton#btnStopScan:hover { background-color: #dc2626; }

        QComboBox {
            background-color: white;
            border: 1px solid #e2e8f0;
            border-radius: 4px;
            padding: 5px 10px;
        }
        QComboBox:hover { border-color: #3b82f6; }
        QComboBox::drop-down { border: none; width: 25px; }
        QComboBox QAbstractItemView {
            background-color: white;
            border: 1px solid #e2e8f0;
            selection-background-color: #3b82f6;
            selection-color: white;
        }

        QLineEdit {
            border: 1px solid #e2e8f0;
            border-radius: 4px;
            padding: 5px 8px;
        }
        QLineEdit:focus { border-color: #3b82f6; }

        QTableWidget {
            background-color: white;
            alternate-background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            gridline-color: #f1f5f9;
            selection-background-color: #e0e7ff;
            selection-color: #1e293b;
        }
        QTableWidget::item { padding: 4px 6px; }

        QHeaderView::section {
            background-color: #334155;
            color: white;
            padding: 6px 8px;
            border: none;
            border-right: 1px solid #475569;
            font-weight: bold;
            font-size: 12px;
        }
        QHeaderView::section:last { border-right: none; }

        QCheckBox { spacing: 6px; }
        QCheckBox::indicator {
            width: 16px; height: 16px;
            border-radius: 3px;
            border: 1px solid #cbd5e1;
            background-color: white;
        }
        QCheckBox::indicator:checked {
            background-color: #3b82f6;
            border-color: #3b82f6;
        }

        QTableWidget::indicator {
            width: 16px; height: 16px;
            border-radius: 3px;
            border: 1px solid #cbd5e1;
            background-color: white;
        }
        QTableWidget::indicator:checked {
            background-color: #3b82f6;
            border-color: #3b82f6;
        }

        QProgressBar {
            border: 1px solid #e2e8f0;
            border-radius: 4px;
            text-align: center;
            height: 20px;
            font-size: 11px;
        }
        QProgressBar::chunk {
            background-color: #3b82f6;
            border-radius: 3px;
        }
    """)
    
    global ACTIVE_ENCODER_NAMES
    available = probe_available_encoder_names()
    if available:
        active = [e.display_name for e in ENCODERS if e.name in available]
        if active:
            ACTIVE_ENCODER_NAMES = active

    win = VideoScanner()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
