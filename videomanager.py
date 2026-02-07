# -*- coding:utf-8 -*-
import sys
import os
import json
import subprocess
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QFileDialog, QTableWidget, QTableWidgetItem,
    QMessageBox, QHBoxLayout, QCheckBox, QComboBox, QLabel, QLineEdit,
    QMenu, QProgressBar, QDialog, QTextEdit
)
import psutil
import re

VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".mts", ".m2ts", ".rm", ".rmvb", ".mpg",
    ".mpeg", ".vob", ".3gp", ".f4v", ".asf", ".ogv", ".dv"
)
CONFIG_FILE = "config.json"
TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "webvtt"}

def analyze_video(path, cache=None):
    try:
        stat = os.stat(path)
    except:
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

    info = {
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
# cache
# =======================
def load_cache():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_cache(cache):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# =======================
# ffprobe
# =======================
def probe_streams_detail(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_streams",
        "-of", "json",
        path
    ]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        data = json.loads(r.stdout)

        audio = 0
        sub_streams = []

        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                audio += 1
            elif s.get("codec_type") == "subtitle":
                sub_streams.append(s)

        return audio, sub_streams
    except:
        return 1, []

def probe_resolution(path):
    """
    返回 width, height
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        data = json.loads(r.stdout)
        s = data["streams"][0]
        return int(s["width"]), int(s["height"])
    except:
        return 1920, 1080  # fallback
    
def detect_animation(path, seconds=20):
    """
    True = 动画
    False = 实拍
    """
    try:
        cmd = [
            "ffmpeg", "-v", "error",
            "-t", str(seconds),
            "-i", path,
            "-vf", "signalstats",
            "-f", "null", "-"
        ]
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        entropy_vals = []
        for line in p.stderr.splitlines():
            if "entropy" in line:
                m = re.search(r"entropy:\s*([0-9.]+)", line)
                if m:
                    entropy_vals.append(float(m.group(1)))

        if not entropy_vals:
            return False  # 默认实拍

        avg_entropy = sum(entropy_vals) / len(entropy_vals)

        return avg_entropy <= 2.5
    except:
        return False
    
def pick_ref_bframes(width, height):
    pixels = width * height
    if pixels <= 1280 * 720:
        return 6, 8
    elif pixels <= 1920 * 1080:
        return 5, 8
    elif pixels <= 2560 * 1440:
        return 4, 6
    else:
        return 3, 4

def probe_audio_sub_count(path):
    """
    返回：audio_count, subtitle_count
    """
    audio_cnt = 0
    sub_cnt = 0
    try:
        audio_cnt, sub_streams = probe_streams_detail(path)
        sub_cnt = len(sub_streams)
    except:
        pass
    return audio_cnt, sub_cnt

def probe_video_quality(path):
    """
    返回：
    codec, bitrate_kbps
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,bit_rate",
                "-of", "json",
                path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        data = json.loads(r.stdout)
        s = data["streams"][0]
        codec = s.get("codec_name", "unknown")
        br = s.get("bit_rate")
        bitrate_kbps = int(br) // 1000 if br and br.isdigit() else 0
        return codec, bitrate_kbps
    except:
        return "unknown", 0

def evaluate_compress_value(codec, bitrate_kbps, mb_per_min):
    """
    返回：
    score (0-100), 预估节省百分比
    """
    score = 0

    # 编码器权重
    if codec in ("mpeg4", "xvid", "divx"):
        score += 40
    elif codec in ("h264", "avc"):
        score += 25
    elif codec in ("hevc", "h265", "av1", "vp9"):
        score -= 30

    # 码率权重
    if bitrate_kbps > 6000:
        score += 30
    elif bitrate_kbps > 3500:
        score += 15
    elif bitrate_kbps < 2500:
        score -= 20

    # 体积权重
    if mb_per_min > 80:
        score += 30
    elif mb_per_min > 50:
        score += 15
    elif mb_per_min < 40:
        score -= 20

    score = max(0, min(score, 100))

    # 预估节省率
    if score >= 70:
        save_pct = 60
    elif score >= 50:
        save_pct = 40
    elif score >= 30:
        save_pct = 25
    else:
        save_pct = 10

    return score, save_pct

def get_video_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return float(r.stdout.strip())
    except:
        return 0

# =======================
# 扫描线程
# =======================
class ScanThread(QThread):
    video_found = pyqtSignal(dict)
    scan_finished = pyqtSignal()

    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.cache = load_cache()
        self._stop = False   # ✅ 新增

    def stop(self):
        self._stop = True   # ✅ 新增
    
    def run(self):
        for root, _, files in os.walk(self.folder):
            if self._stop:
                break
            
            for name in files:
                if self._stop:
                    break
                
                if not name.lower().endswith(VIDEO_EXTS):
                    continue
                path = os.path.abspath(os.path.join(root, name))
                try:
                    stat = os.stat(path)
                except:
                    continue
                size = stat.st_size
                mtime = stat.st_mtime
                cached = self.cache.get(path)
                if cached and cached["size"] == size and cached["mtime"] == mtime:
                    self.video_found.emit(cached)
                    continue
                duration = get_video_duration(path)
                if duration <= 0:
                    continue
                size_mb = size / 1024 / 1024
                mb_per_min = size_mb / (duration / 60)
                codec, bitrate_kbps = probe_video_quality(path)
                info = analyze_video(path, self.cache)
                if info:
                    self.video_found.emit(info)
        
        if not self._stop:
            save_cache(self.cache)
        self.scan_finished.emit()

class ConvertLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("后台正在转换...")
        self.resize(500, 400)
        layout = QVBoxLayout(self)

        self.label_file = QLabel("当前视频进度: 0%")
        self.label_total = QLabel("总体进度: 0%")
        self.progress_file = QProgressBar()
        self.progress_total = QProgressBar()
        self.progress_file.setFormat("当前视频: %p%")
        self.progress_total.setFormat("总体进度: %p%")

        self.text_log = QTextEdit()
        self.text_log.setReadOnly(True)

        layout.addWidget(self.label_file)
        layout.addWidget(self.progress_file)
        layout.addWidget(self.label_total)
        layout.addWidget(self.progress_total)
        layout.addWidget(QLabel("后台日志:"))
        layout.addWidget(self.text_log)

    def append_log(self, msg: str):
        self.text_log.append(msg)

    def update_progress(self, file_pct: int, total_pct: int):
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
    
    def __init__(self, files, delete_source=False, encoder="libx264", crf=21):
        super().__init__()
        self.files = files
        self.delete_source = delete_source
        self.encoder = encoder
        self.crf = int(crf)
        self._pause = False
        self._stop = False
        self._process = None
        self._current_output = None
    
    def pause(self):
        if self._process and not self._pause:
            try:
                psutil.Process(self._process.pid).suspend()
                self._pause = True
            except psutil.NoSuchProcess:
                pass
    
    def resume(self):
        if self._process and self._pause:
            try:
                psutil.Process(self._process.pid).resume()
                self._pause = False
            except psutil.NoSuchProcess:
                pass
    
    def stop(self):
        self._stop = True
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
            except Exception:
                pass
    
    def run(self):
        total = len(self.files)
        
        enc_tag = {
            "libx264": "x264",
            "libx265": "x265",
            "libvpx-vp9": "vp9",
            "libaom-av1": "av1",
        }.get(self.encoder, self.encoder)
        
        for idx, src in enumerate(self.files, start=1):
            if self._stop:
                break
            
            duration_src = get_video_duration(src)
            if duration_src <= 0:
                continue
            
            base, _ = os.path.splitext(src)
            dst = f"{base}_{enc_tag}.mkv"
            self._current_output = dst
            
            width, height = probe_resolution(src)
            ref, bframes = pick_ref_bframes(width, height)
            is_animation = detect_animation(src)
            tune_hint = "animation" if is_animation else "film"
            
            x264_params = (
                f"ref={ref}:"
                f"bframes={bframes}:b-adapt=2:"
                    "me=umh:subme=10:"
                    "rc-lookahead=50:"
                    "trellis=2:"
                    "aq-mode=3:aq-strength=1.1:"
                    "psy-rd=1.0\:-0.15"
                    "deblock=-1\:-1"
            )
            
            self.log.emit(
                f"参数: {width}x{height} | "
                f"{'动画' if is_animation else '实拍'} | "
                f"encoder={self.encoder} | crf={self.crf}"
            )
            
            cmd = [
                "ffmpeg", "-y",
                "-i", src,
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "0:s?",
            ]
            if self.encoder == "libx264":
                cmd += [
                    "-c:v", "libx264",
                    "-crf", str(self.crf),
                    "-preset", "slow",
                    "-tune", tune_hint,
                    "-x264-params", x264_params,
                ]
            elif self.encoder == "libx265":
                cmd += ["-c:v", "libx265", "-crf", str(self.crf), "-preset", "slow"]
                if is_animation:
                    cmd += ["-tune", "animation"]  # 实拍就别加 tune 了
                cmd += ["-x265-params", "log-level=error"]
            elif self.encoder == "libvpx-vp9":
                cmd += [
                    "-c:v", "libvpx-vp9",
                    "-crf", str(self.crf),
                    "-b:v", "0",
                    "-deadline", "good",
                    "-cpu-used", "2",
                    "-row-mt", "1",
                ]
            elif self.encoder == "libaom-av1":
                cmd += [
                    "-c:v", "libaom-av1",
                    "-crf", str(self.crf),
                    "-b:v", "0",
                    "-cpu-used", "6",
                    "-row-mt", "1",
                    "-tiles", "2x2",
                    "-strict", "-2",  # 启用实验性编码器
                ]
            else:
                cmd += [
                    "-c:v", self.encoder,
                    "-crf", str(self.crf),
                ]
            cmd += [
                "-c:a", "copy",
                "-c:s", "copy",
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-progress", "pipe:1",
                "-nostats",
                dst
            ]
            
            self.log.emit(f"开始压缩: {os.path.basename(src)}")
            p = subprocess.Popen(cmd,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 stdin=subprocess.PIPE,
                                 encoding="utf-8",
                                 errors="ignore",
                                 creationflags=subprocess.CREATE_NO_WINDOW
                                 )
            self._process = p
            
            for line in p.stdout:
                if self._stop:
                    p.terminate()
                    break
                if line.startswith("out_time_ms="):
                    value = line.split("=", 1)[1].strip()
                    if not value.isdigit():
                        continue
                    out_ms = int(value)
                    percent = min(int(out_ms / (duration_src * 1_000_000) * 100), 100)
                    total_percent = int(((idx - 1) + percent / 100) / total * 100)
                    self.progress.emit(percent, total_percent)
            
            p.wait()
            if p.returncode != 0:
                err = ""  # 没有单独的 stderr 了，可以从 stdout 累积或者干脆不读
                self.log.emit(f"ffmpeg 失败：{err}")
                continue
            if os.path.getsize(dst) == 0:
                self.log.emit("输出文件为空，压缩失败")
                continue
            self.output_ready.emit(src, dst)
            if not self._stop and os.path.exists(dst):
                self.output_ready.emit(src, dst)
                if self.delete_source:
                    try:
                        os.remove(src)
                    except Exception:
                        pass
        
        if self._stop and self._current_output:
            try:
                if os.path.exists(self._current_output):
                    os.remove(self._current_output)
                    self.log.emit(f"已删除未完成文件: {os.path.basename(self._current_output)}")
            except Exception:
                self.log.emit(f"无法删除残留文件: {os.path.basename(self._current_output)}")
        
        self.finished.emit()

# =======================
# GUI
# =======================
class VideoScanner(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("视频扫描 & 一键压缩")
        self.resize(1000, 550)
        layout = QVBoxLayout(self)
        btn_layout = QHBoxLayout()
        self.btn_scan = QPushButton("扫描文件夹")
        self.btn_stop_scan = QPushButton("停止扫描")  # ✅ 新增
        self.btn_import = QPushButton("导入视频文件")
        self.btn_import.clicked.connect(self.import_files)
        btn_layout.addWidget(self.btn_import)
        self.btn_compress = QPushButton("压缩勾选视频")
        self.combo_encoder = QComboBox()
        self.combo_encoder.addItems([
            "libx264 (H.264)",
            "libx265 (H.265/HEVC)",
            "libvpx-vp9 (VP9)",
            "libaom-av1 (AV1)"
        ])
        self.btn_stop_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.select_folder)
        self.btn_compress.clicked.connect(self.compress_checked)
        btn_layout.addWidget(self.btn_scan)
        btn_layout.addWidget(self.btn_stop_scan)
        btn_layout.addWidget(self.btn_compress)
        btn_layout.addWidget(self.combo_encoder)
        self.label_crf = QLabel("CRF")
        btn_layout.addWidget(self.label_crf)
        self.lineEdit_crf = QLineEdit()
        self.encoder_default_crf = {
            "libx264 (H.264)": "21",  # 常用 18-23
            "libx265 (H.265/HEVC)": "23",  # 常用 20-28（同质量比 x264 往往要更高一点）
            "libvpx-vp9 (VP9)": "33",  # 常用 28-36（VP9 的 CRF 刻度不同）
            "libaom-av1 (AV1)": "32",  # 常用 28-40（AV1 的 CRF 刻度不同）
        }
        self.lineEdit_crf.setText(self.encoder_default_crf.get(self.combo_encoder.currentText(), "21"))
        self.combo_encoder.currentTextChanged.connect(self.on_encoder_changed)
        btn_layout.addWidget(self.lineEdit_crf)
        self.btn_pause = QPushButton("暂停")
        self.btn_resume = QPushButton("继续")
        self.btn_stop = QPushButton("停止")
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(False)

        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_resume)
        btn_layout.addWidget(self.btn_stop)
        self.btn_pause.clicked.connect(self.pause_compress)
        self.btn_stop_scan.clicked.connect(self.stop_scan)
        self.btn_resume.clicked.connect(self.resume_compress)
        self.btn_stop.clicked.connect(self.stop_compress)
        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(
            [
                "✔",
                "文件名",
                "大小(MB)",
                "时长(分钟)",
                "MB/分钟",
                "音轨",
                "字幕",
                "编码",
                "压缩价值",
                "预计节省",
                "路径",
                "输出文件",
            ]
        )
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(5, 60)  # 音轨
        self.table.setColumnWidth(6, 60)  # 字幕
        self.table.setColumnWidth(7, 60)  # 编码
        self.table.setColumnWidth(8, 80)  # 星级
        self.table.setColumnWidth(9, 80)  # 节省率
        self.table.setColumnWidth(10, 350)  # 路径
        self.table.setColumnWidth(11, 350)
        layout.addLayout(btn_layout)
        layout.addWidget(self.table)
        self.thread = None
        self.compress_thread = None
        self.log_dialog = None
        self.load_history()
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.progress_file = QProgressBar()
        self.progress_total = QProgressBar()
        self.progress_file.setFormat("当前视频: %p%")
        self.progress_total.setFormat("总体进度: %p%")

        layout.addWidget(self.progress_file)
        layout.addWidget(self.progress_total)
        self.chk_delete_source = QCheckBox("转换成功后删除源文件")
        self.chk_delete_source.setChecked(False)

        btn_layout.addWidget(self.chk_delete_source)
    
    def on_encoder_changed(self, text: str):
        self.lineEdit_crf.setText(self.encoder_default_crf.get(text, "21"))
    
    def stop_scan(self):
        if self.thread:
            self.thread.stop()
            self.btn_stop_scan.setEnabled(False)

    def pause_compress(self):
        if self.compress_thread:
            self.compress_thread.pause()
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(True)

    def resume_compress(self):
        if self.compress_thread:
            self.compress_thread.resume()
            self.btn_resume.setEnabled(False)
            self.btn_pause.setEnabled(True)

    def stop_compress(self):
        if self.compress_thread:
            reply = QMessageBox.question(
                self,
                "确认停止",
                "确定要停止当前压缩任务？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.compress_thread.stop()

    def show_context_menu(self, pos):
        menu = QMenu(self)

        delete_action = menu.addAction("从列表中删除")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))

        if action == delete_action:
            self.delete_selected_rows()


    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频文件", "",
                                                "视频文件 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.mts *.m2ts *.rm *.rmvb *.mpg *.mpeg *.vob *.3gp *.f4v *.asf *.ogv *.dv)")
        if not files:
            return
        self.btn_scan.setEnabled(False)
        cache = load_cache()
        for path in files:
            info = analyze_video(path, cache)
            if info:
                self.add_video(info)
        save_cache(cache)
        self.btn_scan.setEnabled(True)
    
    def update_progress(self, file_percent, total_percent):
        self.progress_file.setValue(file_percent)
        self.progress_total.setValue(total_percent)

    def delete_selected_rows(self):
        rows = sorted(set(item.row() for item in self.table.selectedItems()), reverse=True)
        if not rows:
            return

        reply = QMessageBox.question(
            self,
            "确认删除",
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

            # ✅ 从缓存中删除
            if path in cache:
                del cache[path]
                changed = True

            # ✅ 从表格中删除
            self.table.removeRow(row)

        if changed:
            save_cache(cache)

    def load_history(self):
        for v in load_cache().values():
            self.add_video(v)
    
    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择视频目录")
        if not folder:
            return
        
        self.btn_scan.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.btn_stop_scan.setEnabled(True)
        self.btn_compress.setEnabled(False)
        
        self.thread = ScanThread(folder)
        self.thread.video_found.connect(self.add_video)
        self.thread.scan_finished.connect(self.scan_done)
        self.thread.start()
    
    def scan_done(self):
        self.btn_scan.setEnabled(True)
        self.btn_import.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)
        self.btn_compress.setEnabled(True)
    
    def update_output_path(self, src_path, dst_path):
        for row in range(self.table.rowCount()):
            path_item = self.table.item(row, 10)  # 源文件路径列
            if path_item and path_item.text() == src_path:
                self.table.setItem(row, 11, QTableWidgetItem(dst_path))
                break

    def add_video(self, v):
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

        # ===== 新增展示 =====
        codec = v.get("codec", "unknown")
        score = v.get("compress_score", 0)
        save_pct = v.get("save_pct", 0)

        # 星级显示（0-5 星）
        stars = "⭐" * max(1, score // 20) if score > 0 else ""

        self.table.setItem(row, 7, QTableWidgetItem(codec))
        self.table.setItem(row, 8, QTableWidgetItem(stars))
        self.table.setItem(row, 9, QTableWidgetItem(f"~{save_pct}%"))

        # 路径
        self.table.setItem(row, 10, QTableWidgetItem(v["path"]))
        # 输出文件（初始为空）
        self.table.setItem(row, 11, QTableWidgetItem(""))

    def compress_checked(self):
        files = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.checkState() == Qt.CheckState.Checked:
                files.append(self.table.item(row, 10).text())
        if not files:
            QMessageBox.warning(self, "提示", "未选择任何视频")
            return
        self.btn_compress.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.btn_stop_scan.setEnabled(False)
        encoder_map = {
            "libx264 (H.264)": "libx264",
            "libx265 (H.265/HEVC)": "libx265",
            "libvpx-vp9 (VP9)": "libvpx-vp9",
            "libaom-av1 (AV1)": "libaom-av1"
        }
        selected_text = self.combo_encoder.currentText()
        encoder = encoder_map.get(selected_text, "libx264")
        crf = int(self.lineEdit_crf.text().strip() or 0)
        self.compress_thread = CompressThread(
            files,
            delete_source=self.chk_delete_source.isChecked(),
            encoder=encoder,
            crf=crf
        )
        self.compress_thread.finished.connect(self.compress_done)
        self.compress_thread.progress.connect(self.update_progress)
        self.compress_thread.output_ready.connect(self.update_output_path)

        # === 新增：弹出日志对话框 ===
        self.log_dialog = ConvertLogDialog(self)
        self.compress_thread.log.connect(self.log_dialog.append_log)
        self.compress_thread.progress.connect(self.log_dialog.update_progress)
        self.compress_thread.finished.connect(self.log_dialog.accept)
        self.log_dialog.show()

        self.compress_thread.start()
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_resume.setEnabled(False)

    def compress_done(self):
        self.btn_compress.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_import.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.btn_stop.setEnabled(False)
        # 若用户手动关闭了弹窗，这里不重复关闭；未关闭则收起
        if self.log_dialog and self.log_dialog.isVisible():
            self.log_dialog.accept()
        QMessageBox.information(self, "完成", "压缩任务完成")


# =======================
# main
# =======================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = VideoScanner()
    win.show()
    sys.exit(app.exec())