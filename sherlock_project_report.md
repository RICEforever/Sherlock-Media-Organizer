# Sherlock Media Organizer: Project Report

## 1. Project Overview
Sherlock Media Organizer is a comprehensive utility designed to intelligently scan, deduplicate, geocode, and organize massive collections of personal media (photos and videos). Traditional photo organizers often group files rigidly by date or single locations. Sherlock goes further by implementing a state-machine-based "Trip Intelligence" algorithm that simulates human-like understanding of "Home" vs. "Away" trips, resolving complex edge cases like GPS gaps, cross-device multi-user uploads, and long-term stationary periods.

## 2. Core Dependencies
The project leverages Python's ecosystem heavily for performance and metadata extraction:
- **Pillow**: Core image processing and EXIF metadata extraction.
- **pillow-heif**: Apple HEIC/HEIF format support.
- **imagehash**: Perceptual hashing (pHash) to detect visually similar images (e.g., compressed WhatsApp photos vs. original camera photos).
- **geopy**: Distance calculation (geodesic math) for trip segmentation.
- **reverse_geocoder**: Local, rapid offline reverse geocoding (Lat/Lon to City/State) to avoid API rate limits.
- **pandas**: Used for generating a master CSV report of all processed media.
- **folium**: Generates interactive HTML maps for the dashboard to visualize trip locations.
- **xxhash** (optional/accelerator): Ultra-fast non-cryptographic hashing for initial file signature checks.
- **opencv-python** (optional): Used for video perceptual hashing by sampling frames.

## 3. Architecture & Technical Details
The application is modularized into specific functional areas under the `sherlock/` package:

### 3.1 `core/models.py` & `core/database.py`
- **Models**: Defines `MediaFile` and `Trip` dataclasses. 
- **Database (`SherlockDB`)**: Uses SQLite with WAL (Write-Ahead Logging) for high concurrency and performance. It stores media metadata, identified trips, mapping configurations (Device -> Owner, Location -> Nickname), and acts as the central source of truth.

### 3.2 `features/scanner.py`
**Goal**: Rapidly index files and extract metadata.
- **Phase 1: Binary Hashing (Speed)**: Uses `hashlib.sha256` (or `xxhash`). For files > 50MB, it performs a sparse read (Head, Middle, Tail) to generate a fast signature without loading massive 4K videos into memory.
- **Phase 2: Metadata Extraction**: Extracts EXIF data (Date, GPS, Device Maker/Model) from images. For RAW files or videos, it falls back to a subprocess call to `ffprobe`.
- **Perceptual Hashing**: Uses `imagehash` to handle cases where the same photo is compressed or resized.

### 3.3 `features/intelligence.py` (The Brain)
**Goal**: Group thousands of scattered photos into cohesive, logical "Trips".
**The Algorithm (Multi-Dimensional Segmentation)**:
1. **Device Isolation**: Photos are first sorted chronologically *per device/user*.
2. **State Machine (Home vs. Away)**: Tracks if the user is currently at a "Home" location or "Away". 
3. **Transition Triggers**: A continuous segment is immediately fractured/split if:
   - The user transitions from Home to Away.
   - An extreme altitude jump occurs (>300m, indicating a flight or rapid mountain ascent).
   - Time gaps occur (> 72 hours of silence while Away implies a new trip; Home groups allow longer gaps).
   - Distance jumps occur (>100km displacement from the trip's anchor point).
4. **Cross-Device Merging (BFS Graph)**: The system takes fragmented segments across all family members and merges them if they overlap temporally AND spatially. If Dad takes photos on Monday, and Mom takes them on Tuesday in the same city, it fuses them into one unified Family Trip.
5. **Smart Augmentation**: For photos missing GPS (like WhatsApp saves), it interpolates their location if caught tightly between two photos that *do* have GPS.

### 3.4 `features/organizer.py`
**Goal**: Safely move/copy files to the destination drive while removing duplicates.
- **Duplicate Handling**: Retains the highest resolution/largest file among duplicates. Losers are moved to a `discard/` folder rather than permanently deleted, ensuring zero data loss.
- **Storage Management**: Actively monitors disk space. If the primary drive fills up, it prompts the UI for an "Overflow" drive and continues seamlessly.
- **Folder Structure**: Groups by `Location_YYYYMM_Duration_Participants` (for trips) or `Nickname_YYYYMM` (for home periods).

### 3.5 `features/dashboard.py` (Visuals)
- Generates a standalone `dashboard.html` using Bootstrap and Folium, allowing users to visually explore their trips on an interactive map, complete with statistics (Total Pixels, Device Usage, Peak Altitude).

### 3.6 `gui/app.py` 
- Provides the `tkinter` user interface. Uses threaded workers so the UI doesn't freeze during 100GB+ scans.

## 4. Edge Cases Solved

### Handling "The Vacuum" (Interspersed No-GPS Media)
*Problem*: A 2-week vacation in Paris will have hundreds of GPS-tagged iPhone photos, but also dozens of GPS-stripped WhatsApp memes or downloaded tickets interspersed throughout. Standard organizers throw these into an isolated "Unknown Location" folder, ruining the timeline.
*Solution*: The intelligence module's State Machine inherits the active state. If the user is in Paris, GPS-stripped photos are assumed to also belong to the Paris trip until hard evidence (a new GPS tag or massive time gap) proves otherwise.

### The "Overlapping Timelines" Problem
*Problem*: If two different family members go on two different trips at the exact same time, a naive time-based grouper will mix their photos into one chaotic folder.
*Solution*: Segmenting initially by `device_nickname` isolates the timelines. They are only merged later if they share spatial proximity (geodesic distance < 50km).

### Storage Overflow Mid-Transfer
*Problem*: Moving 500GB of video and hitting an "Out of Space" error halfway ruins the structured database state.
*Solution*: `MediaOrganizer._get_target_base()` checks `shutil.disk_usage()` dynamically before moving *every single file*. If full, it pauses the worker threads and prompts the user for an Overflow Disk to continue.

### Rapid Sub-Trip Fragmentation
*Problem*: Taking a 5-day road trip might generate 15 different small cities/towns in the reverse geocoder, creating 15 micro-folders of 3 photos each.
*Solution*: A trip is defined by a primary anchor location. As long as the displacement doesn't exceed the user-defined `trip_distance_km` threshold, those 15 towns are merged into a single multi-location trip named after the most frequent stop.

## 5. Technical Challenges and Takeaways
- **Performance bottleneck with FFmpeg/ffprobe**: Reading metadata from thousands of massive 4K video files via subprocess overhead was slow. *Fix*: We prioritize fast regex date parsing from filenames before falling back to ffprobe. For file uniqueness, sparse-binary hashing reads only 3MB total from any file, regardless of its size.
- **Windows Multiprocessing vs. `reverse_geocoder`**: Using standard `multiprocessing` on Windows with the massive in-memory K-D tree of `reverse_geocoder` caused memory spikes and process crashes. *Fix*: ThreadPoolExecutor handles I/O (file copying), while geocoding is forced to run synchronously or in `mode=1` to prevent memory serialization issues.
- **Dependency Management**: We utilized `try/except` imports for bulky or platform-specific libraries (like `opencv-python` and `pillow-heif`), allowing the app to degrade gracefully rather than fail immediately if the environment is incomplete.

## Conclusion
Sherlock Media Organizer transforms a raw folder of scattered JPEGs into a structured visual biography. By marrying raw file mechanics (Sparse Hashing, Exif extraction) with algorithmic graph grouping (BFS merging, State Machines), it mimics how humans actually remember their lives: dynamically scaling from "Months at Home" to tightly-packed "Weekend Trips," cross-referencing multiple devices into a single unified timeline.
