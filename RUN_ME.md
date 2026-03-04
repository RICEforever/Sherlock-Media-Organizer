# Sherlock Media Organizer - RUN ME Guide

This document provides complete instructions on how to set up and run the Sherlock Media Organizer, along with an explanation of its internal logic.

## 1. Prerequisites

Before running the program, ensure you have the following installed on your system:
- **Python 3.8+**
- **FFmpeg**: Required for accurate video metadata extraction (dates, GPS, resolution).
    - Windows: Download from gyan.dev or use `winget install ffmpeg`.
    - macOS: `brew install ffmpeg`
    - Linux: `sudo apt install ffmpeg`

## 2. Installation

1. Clone or download this repository.
2. Open a terminal/command prompt in the project root directory (`Photos Mapper` folder).
3. (Optional but recommended) Create a virtual environment:
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS/Linux
   source .venv/bin/activate
   ```
4. Install the required Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. *(Optional)* Install OpenCV for video perceptual hashing to detect duplicate videos:
   ```bash
   pip install opencv-python
   ```

## 3. How to Run

Sherlock can be run in two modes: **GUI Mode** (Interactive) and **Headless Mode** (Command Line).

### GUI Mode (Recommended)
This mode opens a visual dashboard where you can add source folders, configure settings, and monitor the organization process.

```bash
python -m sherlock.main
```

1. Click **"Add Source"** to select the folders containing your unorganized photos and videos.
2. Select a **"Destination Folder"** where the organized media will be placed.
3. Configure settings like "Home Cities", "Trip Break Gap (Hours)", and "Trip Distance (km)" in the GUI.
4. Click **"Start Organizing"** to begin.

### Headless Mode (Command Line)
Useful for automated scripts, servers, or running without a visual interface.

```bash
python headless_runner.py <work_dir> <source_dir1,source_dir2> <dest_dir>
```
- `<work_dir>`: Directory where the SQLite database and temporary files will be stored.
- `<source_dir1,source_dir2>`: Comma-separated list of folders to scan.
- `<dest_dir>`: The destination folder for organized files.

Example:
```bash
python headless_runner.py "./work_db" "C:\Dump1,C:\Dump2" "D:\Organized_Photos"
```

## 4. How It Works: The Logic Explained

Sherlock uses a multi-phase, highly intelligent pipeline to process, deduplicate, and organize your media.

### Phase 1: Extraction & Deduplication (`scanner.py`)
1. **Binary Hashing:** Uses fast SHA-256 chunked hashing (or md5/xxhash) to instantly detect exact 1:1 binary duplicate files, significantly reducing processing time.
2. **Perceptual Hashing (pHash):** Uses mathematical image analysis to detect visually identical photos (and optionally videos using OpenCV) even if they have different resolutions or file sizes.
3. **Exhaustive Metadata Extraction:**
   - **Images:** Parses EXIF data using `Pillow` to extract exact capture dates, GPS coordinates, altitude, device Make/Model, and resolution. Handles RAW formats (DNG, CR2, etc.) and HEIC.
   - **Videos:** Uses `FFmpeg` (via `ffprobe`) to accurately parse embedded location tags (ISO6709) and creation dates from MOV/MP4 containers.

### Phase 2: Intelligence & Sorting (`intelligence.py`)
1. **Smart Geocoding:** Converts GPS coordinates into human-readable locations (City, District, State) using localized offline reverse geocoding (`reverse_geocoder`), prioritized against your configured "Home Cities".
2. **Metadata Augmentation:** If photos lack GPS but were taken within a short time frame of photos *with* GPS on the same device, the system interpolates and augments the missing location data.
3. **Multi-Dimensional Trip Segmentation (The Core Logic):**
   - Media is grouped sequentially per device.
   - **State Transitions:** Switches between "Home" and "Away" states.
   - **Time Gaps:** A sequence splits into a new trip if a massive time gap occurs (e.g., > 72 hours of silence while away).
   - **Distance Displacement:** A sequence splits if the physical location jumps significantly (e.g., > 100km) over a short time.
   - **Altitude Jumps:** Recognizes sudden altitude shifts (> 300m) to segment flights or mountain trips.
4. **Cross-Device Merging:** Finally, merges the segmented clusters together across *different* devices (e.g., your phone and your partner's phone) if the photos overlap temporally and spatially, creating a single unified trip folder.

### Phase 3: Organization & AI Context (`organizer.py`)
1. **Folder Structuring:** Groups stationary/home photos by `Month_Year` and groups travel trips by `Location_Date_Duration`.
2. **Safe Moving/Copying:** Physically copies or moves the final unique files into the structured destination. Duplicate files are isolated in a separate `discard` folder so no data is ever permanently deleted by default.
3. **AI Context Export:** Generates a lightweight `sherlock_ai_context.json` file summarizing your library. You can upload this to ChatGPT/Claude to ask complex natural language questions about your memories (e.g., "Summarize my trips to Europe in 2023").
