# Video Converter - 视频压缩工具

基于 PyQt6 + FFmpeg 的视频批量压缩工具，支持多种编码器和硬件加速。

## 功能特性

### 核心功能
- **批量扫描** - 递归扫描文件夹中的所有视频文件
- **多线程分析** - 4线程并行分析视频信息，加速扫描
- **智能压缩评分** - 根据编码、码率、体积自动评估压缩价值（0-100分，0-5星可视化）
- **按价值勾选** - 下拉框按压缩价值筛选勾选：全部、>=1星、>=2星、>=3星、>=4星、>=5星
- **多编码器支持** - libx264、libx265、VP9、AV1
- **硬件加速** - NVIDIA NVENC、Intel QSV 硬件编码
- **两遍编码** - 支持 x264/x265 两遍编码，提升压缩质量

### 操作功能
- **拖拽导入** - 直接拖拽视频文件或文件夹到窗口
- **导入/加载列表** - 支持 JSON 格式的视频列表导入导出
- **输出格式选择** - MP4 或 MKV
- **自定义输出目录** - 可指定压缩后文件的保存位置
- **暂停/继续/停止** - 压缩过程中可随时控制
- **删除源文件** - 压缩成功后自动删除源文件
- **自动关机** - 全部压缩完成后自动关机

### 界面
- 深色表头 + 简洁配色
- 按压缩价值（星级）批量勾选
- 实时进度显示（单文件 + 总体）
- 压缩日志对话框

## 截图

<img width="1102" height="632" alt="image" src="https://github.com/user-attachments/assets/ecfd43d1-f71e-4697-85b9-2121235d058b" />
<img width="1102" height="632" alt="image" src="https://github.com/user-attachments/assets/f596d0e3-4534-4365-85ad-139caae22e27" />

## 环境要求

- Python 3.9+
- FFmpeg（需添加到系统 PATH）

## 安装

```bash
git clone https://github.com/coco54-beep/videomanager.git
cd videomanager
pip install -r requirements.txt
```

## 使用

```bash
python videomanager.py
```

### 操作流程

1. 点击 **"扫描文件夹"** 或 **拖拽文件** 到窗口
2. 通过 **"按价值勾选"** 下拉框批量勾选，或在表格中手动勾选
3. 选择编码器、CRF 值、输出格式
4. 点击 **"压缩选中"** 开始压缩

### 编码器说明

| 编码器 | 速度 | 压缩率 | 说明 |
|--------|------|--------|------|
| libx264 | 快 | 中 | 兼容性最好，参照小丸工具箱优化参数 |
| libx265 | 慢 | 高 | 新一代编码，优化 rd/psy-rd/aq |
| libvpx-vp9 | 中 | 高 | WebM 格式 |
| libaom-av1 | 很慢 | 很高 | 下一代标准 |
| h264_nvenc | 很快 | 中 | NVIDIA 显卡加速 |
| hevc_nvenc | 快 | 高 | NVIDIA 显卡加速 |
| h264_qsv | 很快 | 中 | Intel 核显加速 |
| hevc_qsv | 快 | 高 | Intel 核显加速 |

### x264 优化参数（参照小丸工具箱）

- `preset slow` - 编码速度与压缩率平衡
- `scenecut=60` - 场景切换检测
- `qcomp=0.5` - 码率分配
- `partitions=all` - 宏块分区
- `merange=34` - 运动估计范围
- `rc-lookahead=240` - 前瞻帧数
- `aq-mode=3:aq-strength=0.9` - 自适应量化
- `keyint`/`min-keyint` - 自动根据帧率计算关键帧间隔

### CRF 值参考（参照小丸工具箱）

| 范围 | 说明 |
|------|------|
| **18-22** | 高质量，文件较大（收藏级） |
| **23-24** | 平衡质量与大小（推荐，默认 23） |
| **25-28** | 中等质量，文件较小 |
| **29-35** | 较低质量，极致压缩 |

## 项目结构

```
videomanager/
├── videomanager.py              # GUI 主程序
├── batch_compress_from_video_list.py # 命令行批量压缩脚本
├── config.json                  # 视频信息缓存（自动生成）
├── requirements.txt             # Python 依赖
└── README.md                    # 项目说明
```

## 命令行批量压缩

`batch_compress_from_video_list.py` 是一个独立的命令行脚本，用于根据 `config.json` 中的压缩评分批量处理视频。

### 工作原理

1. 读取 `config.json` 中的视频列表
2. 筛选出 `compress_score > 0` 的视频（即有压缩价值的）
3. 使用 H.264 编码转码每个视频
4. 验证输出文件是否正常
5. 删除源文件
6. 更新 `config.json`

### 使用场景

- 无需 GUI 操作，适合后台批处理
- 定时任务自动压缩
- 服务器环境无人值守运行

### 使用方法

```bash
# 先用 GUI 扫描生成 config.json，然后运行：
python batch_compress_from_video_list.py
```

### 配置修改

脚本顶部可修改以下参数：

```python
CONFIG_PATH = r"config.json"  # 配置文件路径
CRF = "21"                    # 压缩质量
```

输出文件命名格式：`原文件名_h264.mkv`

## 许可证

MIT License