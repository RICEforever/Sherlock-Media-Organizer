# Sherlock Media Organizer v2.0 (Modular)

An intelligent, completely local media organization tool that uses spatial-temporal clustering and reverse geocoding to automatically sort your chaotic photo dumps into perfectly structured trips and events.

## 🚀 Quick Start (Complete Guide in `RUN_ME.md`)

For the full detailed explanation of how to run the software and the internal logic for extraction and sorting, please see **[RUN_ME.md](RUN_ME.md)**.

### Brief Installation
1. Ensure Python 3.8+ and **[FFmpeg](https://ffmpeg.org/download.html)** are installed.
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Running the App
**GUI Interactive Mode:**
```bash
python -m sherlock.main
```
**Headless Background Mode:**
```bash
python headless_runner.py <work_dir> <source_dir1,source_dir2> <dest_dir>
```

---

## ✨ Features Summary

### 1. AI Chatbot Integration
- Scans your entire media library and generates a summarized context file structure.
- Click **"Export AI Context JSON"** in the Dashboard.
- Upload `sherlock_ai_context.json` to ChatGPT or Claude to converse with your history. Ask questions like: *"When was the last time I went to Paris?"* or *"Summarize my hiking trips."*

### 2. Intelligent Trip Detection & Sorting
- **Multi-Dimensional Clustering:** Automatically groups photos into unified trips based on Time gaps (e.g., 72 hours), Distance displacement (> 100km), and Altitude jumps.
- **Cross-Device Sync:** Merges photos taken by different devices (e.g., an iPhone and a DSLR) into the same folder if they occurred at the same time and place.
- **Missing GPS Interpolation:** Intelligently guesses the location of photos with no GPS by analyzing the timestamps of neighboring photos.

### 3. Video Metadata Support & Deduplication
- Uses `ffprobe` to pull exact capture dates and GPS tags out of video containers (MP4, MOV).
- Binary deduplication ensures exact copies are removed instantly.
- (Optional) Perceptual video hashing using OpenCV detects visually duplicated clips even if resolutions differ.
