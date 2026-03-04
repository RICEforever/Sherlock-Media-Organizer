import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from html import escape

import folium
import imagehash
import pandas as pd
import reverse_geocoder as rg
import tkinter as tk
from folium.plugins import MarkerCluster
from geopy.distance import geodesic
from PIL import Image, ExifTags
from tkinter import filedialog, messagebox, ttk

orjson = __import__("orjson") if importlib.util.find_spec("orjson") else None
xxhash = __import__("xxhash") if importlib.util.find_spec("xxhash") else None
if importlib.util.find_spec("pillow_heif"):
    __import__("pillow_heif").register_heif_opener()

APP_NAME = "Sherlock Media Organiser V0.7"
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp', '.heic'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.cr2', '.arw', '.dng', '.3gp'}
MIN_FREE_SPACE_GB = 5
SCAN_WORKERS = max(4, min(16, (os.cpu_count() or 4) * 2))

# Windows reserved names that cannot be used as folder names
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", 
    "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

CSS_TEMPLATE = """
body { font-family: 'Segoe UI', sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
h1 { color: #bb86fc; text-align: center; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 30px; }
.stat-card { background: #1e1e1e; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); text-align: center; }
.stat-val { font-size: 2em; font-weight: bold; color: #03dac6; }
.stat-label { color: #a0a0a0; margin-top: 5px; }
.section-title { border-bottom: 2px solid #333; padding-bottom: 10px; margin-top: 30px; color: #cf6679; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; background: #1e1e1e; }
th, td { padding: 10px; text-align: left; border-bottom: 1px solid #333; }
th { background-color: #2c2c2c; color: #bb86fc; }
tr:hover { background-color: #252525; }
.map-container { height: 500px; width: 100%; border-radius: 8px; overflow: hidden; margin-top: 15px; }
.small { color:#9aa0a6; font-size: 0.9em; }
.security-warning { background: #cf6679; color: white; padding: 10px; border-radius: 4px; margin-bottom: 20px; font-weight: bold; text-align: center; }
"""


class SherlockBrain:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.memory_file = self.db_path / "sherlock_memory.json"
        self.config_file = self.db_path / "sherlock_config.json"
        self.activity_file = self.db_path / "activity_log.json"
        self.data = {}
        self.owners = {}
        self.activity = []
        # Default settings
        self.config = {
            'trip_gap_hours': 72,
            'trip_distance_km': 100,
            'owner_mapping': {}
        }
        self.lock = threading.Lock()
        self.load()

    def _read_json(self, path, default):
        if not path.exists():
            return default
        try:
            raw = path.read_bytes()
            if not raw:
                return default
            return orjson.loads(raw) if orjson else json.loads(raw.decode("utf-8"))
        except Exception:
            return default

    def _write_json_atomic(self, path, payload):
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent) as tmp:
                if orjson:
                    tmp.write(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
                else:
                    tmp.write(json.dumps(payload, indent=2, default=str).encode("utf-8"))
                temp_path = Path(tmp.name)
            os.replace(temp_path, path)
        except Exception as e:
            print(f"Failed to write JSON {path}: {e}")

    def load(self):
        with self.lock:
            self.data = self._read_json(self.memory_file, {})
            saved_config = self._read_json(self.config_file, {})
            self.config.update(saved_config)
            self.owners = self.config.get('owner_mapping', {})
            self.activity = self._read_json(self.activity_file, [])

    def save(self):
        with self.lock:
            self.config['owner_mapping'] = self.owners
            self._write_json_atomic(self.memory_file, self.data)
            self._write_json_atomic(self.config_file, self.config)
            self._write_json_atomic(self.activity_file, self.activity[-5000:])

    def get_duplicate_meta(self, file_hash):
        with self.lock:
            return self.data.get(file_hash)

    def register_file(self, file_hash, meta):
        with self.lock:
            self.data[file_hash] = meta

    def get_owner(self, device_model):
        with self.lock:
            return self.owners.get(device_model, "Unknown")

    def set_owners_bulk(self, mapping):
        with self.lock:
            self.owners.update(mapping)
        self.save()

    def log_activity(self, entry):
        with self.lock:
            entry["logged_at"] = datetime.now().isoformat(timespec="seconds")
            self.activity.append(entry)


class SherlockEngine:
    def __init__(self, brain, status_cb, progress_cb, request_drive_cb=None):
        self.brain = brain
        self.status = status_cb
        self.progress = progress_cb
        self.request_drive_cb = request_drive_cb
        self.df = pd.DataFrame()
        self.stop_event = threading.Event()
        
        # Initialize EXIF tag IDs
        self.TAG_DATETIME = next((k for k, v in ExifTags.TAGS.items() if v == 'DateTimeOriginal'), 36867)
        self.TAG_MODEL = next((k for k, v in ExifTags.TAGS.items() if v == 'Model'), 272)
        self.TAG_GPSINFO = next((k for k, v in ExifTags.TAGS.items() if v == 'GPSInfo'), 34853)

    def request_stop(self):
        self.stop_event.set()

    def get_free_space(self, path):
        target = Path(path)
        if not target.exists():
            target = target.parent
        try:
            return shutil.disk_usage(target).free // (2**30)
        except OSError:
            return 0

    def clean_name(self, name):
        name = str(name).strip()
        # Remove illegal characters
        name = re.sub(r'[<>:"/\\|?*]', '', name)
        # Handle Windows reserved names
        if name.upper() in WINDOWS_RESERVED_NAMES:
            name = f"_{name}"
        return name or "Unknown_Location"

    def _to_float(self, value):
        try:
            return float(value)
        except Exception:
            num = getattr(value, "numerator", None)
            den = getattr(value, "denominator", None)
            if num is not None and den:
                return float(num) / float(den)
        return None

    def _gps_to_decimal(self, values, ref):
        if not values or len(values) < 3:
            return None
        parts = [self._to_float(v) for v in values[:3]]
        if any(v is None for v in parts):
            return None
        decimal = parts[0] + (parts[1] / 60.0) + (parts[2] / 3600.0)
        if str(ref).upper() in {"S", "W"}:
            decimal = -decimal
        return decimal

    def _fast_digest(self, filepath):
        """Quickly hash a file using chunks from start, middle, and end."""
        hasher = xxhash.xxh3_128() if xxhash else hashlib.blake2b(digest_size=16)
        try:
            with open(filepath, 'rb') as f:
                # First 64KB
                hasher.update(f.read(65536))
                f.seek(0, 2)
                size = f.tell()
                if size > 131072:
                    # Middle 64KB
                    f.seek(size // 2)
                    hasher.update(f.read(65536))
                    # Last 64KB
                    f.seek(max(0, size - 65536))
                    hasher.update(f.read(65536))
            return f"sz:{size}_h:{hasher.hexdigest()}"
        except Exception:
            return None

    def get_dual_hash(self, filepath):
        """Efficiency-first hashing: Fast Hash -> Perceptual Hash (Images only)."""
        ext = Path(filepath).suffix.lower()
        fast = self._fast_digest(filepath)
        if not fast: return None

        if ext in IMAGE_EXTS:
            try:
                with Image.open(filepath) as img:
                    # Combining fast hash with pHash for high accuracy & performance
                    return f"{fast}_{imagehash.phash(img)}"
            except Exception:
                return fast
        return fast

    def get_metadata(self, filepath):
        ext = Path(filepath).suffix.lower()
        stats = os.stat(filepath)
        # On Windows, st_ctime is often creation time. On Unix, it's metadata change.
        date_obj = datetime.fromtimestamp(min(stats.st_mtime, stats.st_ctime))
        device = "Unknown_Device"
        lat, lon, width, height = None, None, 0, 0

        if ext in IMAGE_EXTS:
            try:
                with Image.open(filepath) as img:
                    width, height = img.size
                    exif = img.getexif()
                    if exif:
                        date_raw = exif.get(self.TAG_DATETIME)
                        if date_raw:
                            try:
                                date_obj = datetime.strptime(str(date_raw), '%Y:%m:%d %H:%M:%S')
                            except ValueError: pass
                        
                        model = exif.get(self.TAG_MODEL)
                        if model:
                            device = str(model).strip()
                            
                        gps_info = exif.get_ifd(self.TAG_GPSINFO)
                        if gps_info:
                            # Using GPS tags
                            lat_ref = gps_info.get(1, 'N')
                            lat_val = gps_info.get(2)
                            lon_ref = gps_info.get(3, 'E')
                            lon_val = gps_info.get(4)
                            lat = self._gps_to_decimal(lat_val, lat_ref)
                            lon = self._gps_to_decimal(lon_val, lon_ref)
            except Exception:
                pass
        
        elif ext in VIDEO_EXTS:
            try:
                # Try to parse date from filename first (very fast)
                filename = Path(filepath).name
                date_match = re.search(r'(\d{4})[-_](\d{2})[-_](\d{2})', filename)
                if date_match:
                    try:
                        date_obj = datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
                    except ValueError: pass

                # Use FFprobe for high-fidelity metadata
                cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', filepath]
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
                out = subprocess.check_output(cmd, startupinfo=startupinfo, stderr=subprocess.STDOUT)
                data = json.loads(out)
                
                fmt = data.get('format', {})
                tags = fmt.get('tags', {})
                
                # Date
                c_time = tags.get('creation_time')
                if c_time:
                    try:
                        date_obj = datetime.strptime(c_time.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                    except ValueError: 
                        try:
                            date_obj = datetime.strptime(c_time.split('.')[0], "%Y-%m-%d %H:%M:%S")
                        except ValueError: pass

                # Device
                make = tags.get('make') or tags.get('com.apple.quicktime.make')
                model = tags.get('model') or tags.get('com.apple.quicktime.model')
                if make and model:
                    if str(make).lower() in str(model).lower():
                        device = str(model).strip()
                    else:
                        device = f"{str(make).strip()} {str(model).strip()}"
                elif model:
                    device = str(model).strip()
                elif make:
                    device = str(make).strip()

                # GPS (ISO 6709)
                loc_str = tags.get('location') or tags.get('com.apple.quicktime.location.ISO6709')
                if loc_str:
                    match = re.match(r'([+-][0-9.]+)([+-][0-9.]+)', loc_str)
                    if match:
                        lat = float(match.group(1))
                        lon = float(match.group(2))
            except Exception:
                pass

        return {
            'date': date_obj,
            'device': device,
            'lat': lat,
            'lon': lon,
            'width': width,
            'height': height,
            'size': stats.st_size,
        }

    def _iter_source_files(self, sources):
        for src in sources:
            for root, _, files in os.walk(src):
                for file in files:
                    yield os.path.join(root, file)

    def _scan_file(self, filepath):
        try:
            file_hash = self.get_dual_hash(filepath)
            if not file_hash:
                return None
            meta = self.get_metadata(filepath)
            return {
                'path': filepath,
                'hash': file_hash,
                'size': meta['size'],
                'date': meta['date'],
                'device': meta['device'],
                'lat': meta['lat'],
                'lon': meta['lon'],
                'location': 'Unknown',
                'resolution': meta['width'] * meta['height'],
            }
        except Exception as e:
            self.brain.log_activity({'action': 'scan_error', 'path': filepath, 'error': str(e)})
            return None

    def scan_sources(self, sources):
        self.status("Phase 1/5: Deep scanning source folders...")
        all_files = list(self._iter_source_files(sources))
        if not all_files:
            self.df = pd.DataFrame()
            self.status("No files discovered in source folders.")
            return

        scanned = []
        total = len(all_files)
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futures = [pool.submit(self._scan_file, p) for p in all_files]
            for idx, fut in enumerate(as_completed(futures), start=1):
                if self.stop_event.is_set():
                    self.status("Stop requested. Halting scan.")
                    return
                result = fut.result()
                if result:
                    scanned.append(result)
                if idx % 20 == 0 or idx == total:
                    self.progress(idx, total)

        self.df = pd.DataFrame(scanned)
        self.status(f"Scan complete. {len(self.df)} media files found.")

    def process_intelligence(self):
        self.status("Phase 2/5: Running intelligence (geo + trip inference)...")
        if self.df.empty:
            return

        has_gps = self.df.dropna(subset=['lat', 'lon'])
        if not has_gps.empty:
            rounded = list(zip(has_gps['lat'].round(3), has_gps['lon'].round(3)))
            unique_coords = list(dict.fromkeys(rounded))
            geocoded = rg.search(unique_coords)
            
            geo_map = {}
            for c, r in zip(unique_coords, geocoded):
                cc = r.get('cc', '')
                if cc == 'IN':
                    # Priority for India: District (admin2) -> State (admin1) -> City (name)
                    loc_name = r.get('admin2') or r.get('admin1') or r.get('name')
                else:
                    # Priority for others: City (name) -> State (admin1)
                    loc_name = r.get('name') or r.get('admin1')
                
                geo_map[c] = self.clean_name(loc_name or "Unknown")
            
            self.df.loc[has_gps.index, 'location'] = [geo_map[c] for c in rounded]

        self.df = self.df.sort_values(by='date').reset_index(drop=True)
        trip_ids = []
        current_trip = 0
        last_date = None
        last_coord = None
        
        gap_limit = self.brain.config.get('trip_gap_hours', 72) * 3600
        dist_limit = self.brain.config.get('trip_distance_km', 100)

        for _, row in self.df.iterrows():
            new_trip = False
            if last_date and (row['date'] - last_date).total_seconds() > gap_limit:
                new_trip = True

            if pd.notna(row['lat']) and pd.notna(row['lon']) and last_coord:
                if geodesic((row['lat'], row['lon']), last_coord).km > dist_limit:
                    new_trip = True

            if new_trip:
                current_trip += 1
            trip_ids.append(current_trip)
            last_date = row['date']
            if pd.notna(row['lat']) and pd.notna(row['lon']):
                last_coord = (row['lat'], row['lon'])

        self.df['trip_id'] = trip_ids
        self.df['location'] = self.df['location'].replace('Unknown', pd.NA)
        self.df['location'] = self.df.groupby('trip_id')['location'].ffill().bfill().fillna("Unknown_Location")

    def _pick_destination_root(self, roots, current_idx):
        idx = current_idx
        for _ in range(len(roots)):
            if self.get_free_space(roots[idx]) >= MIN_FREE_SPACE_GB:
                return idx
            idx = (idx + 1) % len(roots)

        if self.request_drive_cb:
            new_drive = self.request_drive_cb()
            if new_drive:
                Path(new_drive).mkdir(parents=True, exist_ok=True)
                Path(new_drive, "Discard_Pile").mkdir(parents=True, exist_ok=True)
                roots.append(new_drive)
                self.status(f"Added new destination: {new_drive}")
                return len(roots) - 1

        self.stop_event.set()
        self.status("Paused: no destination with enough free space.")
        return current_idx

    def execute_organization(self, primary_root, overflow_root):
        self.status("Phase 3/5: Organizing files...")
        if self.df.empty:
            return

        roots = [primary_root, overflow_root]
        for root in roots:
            Path(root).mkdir(parents=True, exist_ok=True)
            Path(root, "Discard_Pile").mkdir(parents=True, exist_ok=True)

        total = len(self.df)
        current_root_idx = 0
        for idx, row in self.df.iterrows():
            if self.stop_event.is_set():
                return

            if idx % 10 == 0 or idx + 1 == total:
                self.progress(idx + 1, total)

            current_root_idx = self._pick_destination_root(roots, current_root_idx)
            if self.stop_event.is_set():
                return
            target_root = roots[current_root_idx]

            duplicate_meta = self.brain.get_duplicate_meta(row['hash'])
            is_duplicate = False
            replaced_low_res = False

            if duplicate_meta and os.path.exists(duplicate_meta.get('path', '')):
                old_res = int(duplicate_meta.get('resolution', 0) or 0)
                new_res = int(row.get('resolution', 0) or 0)
                if new_res > old_res:
                    replaced_low_res = True
                    old_path = Path(duplicate_meta['path'])
                    replace_bin = Path(target_root, "Discard_Pile", "Replaced_Lower_Resolution")
                    replace_bin.mkdir(parents=True, exist_ok=True)
                    try:
                        if old_path.exists():
                            shutil.move(str(old_path), str(replace_bin / old_path.name))
                    except Exception as e:
                        self.brain.log_activity({'action': 'error', 'path': str(old_path), 'error': f"Failed to move low-res: {e}"})
                else:
                    is_duplicate = True

            if is_duplicate:
                dest_folder = Path(target_root, "Discard_Pile", "Duplicates")
            else:
                dest_folder = Path(target_root, row['location'], row['date'].strftime('%Y-%m'))
            dest_folder.mkdir(parents=True, exist_ok=True)

            src = Path(row['path'])
            dest = dest_folder / src.name
            if dest.exists():
                dest = dest_folder / f"{src.stem}_{int(time.time())}{src.suffix}"

            try:
                # Use shutil.move for efficiency and atomicity where possible
                shutil.move(str(src), str(dest))
                
                if not is_duplicate:
                    owner = self.brain.get_owner(row['device'])
                    self.brain.register_file(row['hash'], {
                        'path': str(dest),
                        'date': row['date'].isoformat(),
                        'device': row['device'],
                        'owner': owner,
                        'location': row['location'],
                        'lat': float(row['lat']) if pd.notna(row['lat']) else None,
                        'lon': float(row['lon']) if pd.notna(row['lon']) else None,
                        'resolution': int(row.get('resolution', 0) or 0),
                    })
                    self.brain.log_activity({
                        'action': 'import',
                        'path': str(dest),
                        'device': row['device'],
                        'owner': owner,
                        'location': row['location'],
                        'replaced_low_res': replaced_low_res,
                    })
                else:
                    self.brain.log_activity({'action': 'duplicate_discard', 'path': str(dest)})
            except Exception as exc:
                self.brain.log_activity({'action': 'error', 'path': str(src), 'error': str(exc)})

        self.brain.save()
        self.cleanup_empty_folders(self.df['path'].tolist())

    def generate_master_csv(self, db_path):
        self.status("Phase 5/6: Generating master CSV directory...")
        db_dir = Path(db_path)
        csv_path = db_dir / "master_media_directory.csv"

        records = []
        for file_hash, meta in self.brain.data.items():
            row = {'hash': file_hash}
            row.update(meta)
            records.append(row)

        if records:
            df_master = pd.DataFrame(records)
            cols = ['hash', 'path', 'date', 'device', 'owner', 'location', 'lat', 'lon', 'resolution']
            present_cols = [c for c in cols if c in df_master.columns]
            df_master = df_master[present_cols]
            df_master.to_csv(csv_path, index=False)
            self.brain.log_activity({'action': 'csv_generated', 'path': str(csv_path)})
        else:
            self.status("No records found to export to CSV.")

    def cleanup_empty_folders(self, file_paths):
        self.status("Phase 4/5: Cleaning empty source folders...")
        folders = {str(Path(p).parent) for p in file_paths}
        for folder in sorted(folders, key=len, reverse=True):
            try:
                if os.path.exists(folder) and not os.listdir(folder):
                    os.rmdir(folder)
            except OSError:
                continue

    def generate_dashboard(self, db_path):
        self.status("Phase 6/6: Generating dashboard...")
        db_dir = Path(db_path)
        db_dir.mkdir(parents=True, exist_ok=True)

        (db_dir / "style.css").write_text(CSS_TEMPLATE, encoding="utf-8")

        records = list(self.brain.data.values())
        map_records = []
        for r in records:
            lat = r.get('lat')
            lon = r.get('lon')
            if lat is None or lon is None:
                continue
            try:
                lat = float(lat)
                lon = float(lon)
            except (TypeError, ValueError):
                continue
            map_records.append({
                'lat': lat,
                'lon': lon,
                'location': self.clean_name(r.get('location', 'Unknown_Location')),
                'owner': r.get('owner', 'Unknown'),
                'date': r.get('date', ''),
                'path': r.get('path', ''),
                'device': r.get('device', 'Unknown_Device'),
            })

        # fallback to current run dataframe if old memory entries don't carry GPS yet
        if not map_records and not self.df.empty:
            for _, row in self.df.dropna(subset=['lat', 'lon']).iterrows():
                map_records.append({
                    'lat': float(row['lat']),
                    'lon': float(row['lon']),
                    'location': self.clean_name(row.get('location', 'Unknown_Location')),
                    'owner': self.brain.get_owner(row.get('device', 'Unknown_Device')),
                    'date': str(row.get('date', '')),
                    'path': row.get('path', ''),
                    'device': row.get('device', 'Unknown_Device'),
                })

        map_path = db_dir / "map_widget.html"
        if map_records:
            map_records.sort(key=lambda x: x.get('date', ''))
            start = [map_records[0]['lat'], map_records[0]['lon']]
            media_map = folium.Map(location=start, zoom_start=4, tiles="cartodb dark_matter")
            cluster = MarkerCluster().add_to(media_map)
            coords = []
            for rec in map_records:
                coords.append([rec['lat'], rec['lon']])
                file_uri = ''
                try:
                    if rec['path']:
                        file_uri = Path(rec['path']).resolve().as_uri()
                except Exception:
                    file_uri = ''

                popup_html = (
                    f"<b>{escape(rec['location'])}</b><br>"
                    f"Owner: {escape(str(rec['owner']))}<br>"
                    f"Device: {escape(str(rec['device']))}<br>"
                    f"Date: {escape(str(rec['date']))}<br>"
                )
                if file_uri:
                    popup_html += f"<a href=\"{file_uri}\" target=\"_blank\">Open media file</a>"
                else:
                    popup_html += "File path unavailable"

                folium.Marker(
                    [rec['lat'], rec['lon']],
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip=escape(rec['location'])
                ).add_to(cluster)

            if len(coords) > 1:
                folium.PolyLine(coords, color="#03dac6", weight=2, opacity=0.7).add_to(media_map)
            media_map.save(map_path)
        else:
            map_path.write_text(
                "<html><body style='background:#121212;color:#ddd;'>No GPS data available in memory or this run.</body></html>",
                encoding="utf-8"
            )

        records = list(self.brain.data.values())
        owners = pd.Series([r.get('owner', 'Unknown') for r in records]).value_counts().head(5)
        locations = pd.Series([r.get('location', 'Unknown') for r in records]).value_counts().head(5)
        recent = list(reversed(self.brain.activity[-20:]))

        owner_rows = "".join(f"<tr><td>{o}</td><td>{c}</td></tr>" for o, c in owners.items()) or "<tr><td colspan='2'>No data</td></tr>"
        loc_rows = "".join(f"<tr><td>{l}</td><td>{c}</td></tr>" for l, c in locations.items()) or "<tr><td colspan='2'>No data</td></tr>"
        recent_rows = "".join(
            f"<tr><td>{r.get('logged_at','')}</td><td>{r.get('action','')}</td><td>{self.clean_name(r.get('location','-'))}</td><td>{Path(r.get('path','')).name}</td></tr>"
            for r in recent
        ) or "<tr><td colspan='4'>No activity</td></tr>"

        html = f"""
        <html><head><link rel=\"stylesheet\" href=\"style.css\"></head>
        <body>
            <div class=\"security-warning\">
                ⚠️ SECURITY NOTICE: This dashboard contains absolute local file paths. 
                Do not share this file or host it publicly. Use for local indexing only.
            </div>
            <h1>Sherlock Media Dashboard</h1>
            <div class=\"stats-grid\">
                <div class=\"stat-card\"><div class=\"stat-val\">{len(records)}</div><div class=\"stat-label\">Indexed Memories</div></div>
                <div class=\"stat-card\"><div class=\"stat-val\">{len(owners)}</div><div class=\"stat-label\">Top Owners (shown)</div></div>
                <div class=\"stat-card\"><div class=\"stat-val\">{len(locations)}</div><div class=\"stat-label\">Top Locations (shown)</div></div>
            </div>

            <div class=\"section-title\">Global Travel Map + Path</div>
            <div class=\"map-container\"><iframe src=\"map_widget.html\" width=\"100%\" height=\"100%\" frameborder=\"0\"></iframe></div>
            <p class=\"small\">Polyline shows temporal order across GPS-tagged media.</p>

            <div class=\"section-title\">Top Owners</div>
            <table><tr><th>Owner</th><th>Items</th></tr>{owner_rows}</table>

            <div class=\"section-title\">Top Locations</div>
            <table><tr><th>Location</th><th>Items</th></tr>{loc_rows}</table>

            <div class=\"section-title\">Recent Activity</div>
            <table><tr><th>Time</th><th>Action</th><th>Location</th><th>File</th></tr>{recent_rows}</table>
        </body></html>
        """

        dashboard_path = db_dir / "dashboard.html"
        dashboard_path.write_text(html, encoding="utf-8")
        webbrowser.open(str(dashboard_path))


class DeviceTableWindow(tk.Toplevel):
    def __init__(self, parent, devices, callback):
        super().__init__(parent)
        self.title("Map Devices to Owners")
        self.geometry("560x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.entries = {}

        tk.Label(self, text="Assign each device to an owner", font=("Segoe UI", 11, "bold")).pack(pady=10)
        frame = tk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        for dev in sorted(devices):
            row = tk.Frame(frame)
            row.pack(fill=tk.X, pady=4)
            tk.Label(row, text=dev, width=34, anchor='w').pack(side=tk.LEFT)
            entry = tk.Entry(row)
            entry.pack(side=tk.RIGHT, fill=tk.X, expand=True)
            self.entries[dev] = entry

        btn_bar = tk.Frame(self)
        btn_bar.pack(fill=tk.X, padx=20, pady=12)
        tk.Button(btn_bar, text="Save", bg="#2e7d32", fg="white", command=self._save).pack(side=tk.RIGHT)

    def _save(self):
        mapping = {dev: ent.get().strip() for dev, ent in self.entries.items() if ent.get().strip()}
        self.callback(mapping)
        self.destroy()


class SherlockApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("980x820")
        self.root.configure(bg="#121212")

        self.sources = []
        self.engine = None

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", thickness=22, troughcolor="#2b2b2b", background="#03dac6")

        tk.Label(root, text=f"🕵️ {APP_NAME}", font=("Segoe UI", 17, "bold"), bg="#121212", fg="#bb86fc", pady=12).pack(fill=tk.X)

        src_frame = tk.LabelFrame(root, text="1) Source folders", padx=10, pady=10, bg="#121212", fg="#e0e0e0")
        src_frame.pack(fill=tk.X, padx=20, pady=6)
        self.src_list = tk.Listbox(src_frame, height=5, bg="#1e1e1e", fg="#e0e0e0", selectbackground="#333")
        self.src_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        src_buttons = tk.Frame(src_frame, bg="#121212")
        src_buttons.pack(side=tk.LEFT, padx=10)
        tk.Button(src_buttons, text="+ Add", command=self.add_source).pack(fill=tk.X, pady=2)
        tk.Button(src_buttons, text="- Remove", command=self.remove_source).pack(fill=tk.X, pady=2)

        dest = tk.LabelFrame(root, text="2) Destinations", padx=10, pady=10, bg="#121212", fg="#e0e0e0")
        dest.pack(fill=tk.X, padx=20, pady=6)
        self.var_prim = tk.StringVar()
        self.var_over = tk.StringVar()
        self.var_brain = tk.StringVar()

        tk.Label(dest, text="Primary drive:", bg="#121212", fg="#e0e0e0").grid(row=0, column=0, sticky="w")
        tk.Entry(dest, textvariable=self.var_prim, width=66).grid(row=0, column=1, padx=5)
        tk.Button(dest, text="Browse", command=lambda: self.browse(self.var_prim)).grid(row=0, column=2)

        tk.Label(dest, text="Overflow drive:", bg="#121212", fg="#e0e0e0").grid(row=1, column=0, sticky="w", pady=4)
        tk.Entry(dest, textvariable=self.var_over, width=66).grid(row=1, column=1, padx=5)
        tk.Button(dest, text="Browse", command=lambda: self.browse(self.var_over)).grid(row=1, column=2)

        brain = tk.LabelFrame(root, text="3) Sherlock data", padx=10, pady=10, bg="#121212", fg="#e0e0e0")
        brain.pack(fill=tk.X, padx=20, pady=6)
        tk.Label(brain, text="Save Sherlock_Data in:", bg="#121212", fg="#e0e0e0").pack(anchor='w')
        tk.Entry(brain, textvariable=self.var_brain, width=78).pack(side=tk.LEFT, padx=4)
        tk.Button(brain, text="Browse", command=lambda: self.browse(self.var_brain)).pack(side=tk.LEFT)

        self.status_label = tk.Label(root, text="Ready", fg="#03dac6", bg="#121212", anchor='w')
        self.status_label.pack(fill=tk.X, padx=20, pady=6)
        self.progress = ttk.Progressbar(root, orient=tk.HORIZONTAL, mode='determinate')
        self.progress.pack(fill=tk.X, padx=20)

        actions = tk.Frame(root, bg="#121212")
        actions.pack(fill=tk.X, padx=20, pady=18)
        self.start_btn = tk.Button(actions, text="Start Organization", bg="#03dac6", fg="black", font=("Segoe UI", 11, "bold"), command=self.start)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = tk.Button(actions, text="Stop", bg="#cf6679", fg="white", font=("Segoe UI", 11, "bold"), command=self.stop, state="disabled")
        self.stop_btn.pack(side=tk.LEFT, padx=4)

    def add_source(self):
        folder = filedialog.askdirectory()
        if folder and folder not in self.sources:
            self.sources.append(folder)
            self.src_list.insert(tk.END, folder)

    def remove_source(self):
        selected = list(self.src_list.curselection())
        for index in reversed(selected):
            self.sources.pop(index)
            self.src_list.delete(index)

    def browse(self, target_var):
        folder = filedialog.askdirectory()
        if folder:
            target_var.set(folder)

    def status(self, msg):
        self.root.after(0, lambda: self.status_label.config(text=msg))

    def set_progress(self, value, total):
        self.root.after(0, lambda: self.progress.configure(maximum=total, value=value))

    def request_third_drive(self):
        event = threading.Event()
        answer = {'path': None}

        def _prompt():
            msg = "Primary and overflow are full. Select a third destination drive?"
            if messagebox.askyesno("Storage Alert", msg):
                answer['path'] = filedialog.askdirectory(title="Select new destination drive")
            event.set()

        self.root.after(0, _prompt)
        event.wait()
        return answer['path']

    def stop(self):
        if self.engine:
            self.engine.request_stop()
            self.status("Stop requested. Waiting for safe checkpoint...")

    def start(self):
        if not self.sources or not self.var_prim.get() or not self.var_over.get() or not self.var_brain.get():
            messagebox.showerror("Missing input", "Please add sources, destinations, and Sherlock data path.")
            return

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            brain_path = os.path.join(self.var_brain.get(), "Sherlock_Data")
            os.makedirs(brain_path, exist_ok=True)

            brain = SherlockBrain(brain_path)
            self.engine = SherlockEngine(brain, self.status, self.set_progress, self.request_third_drive)

            self.engine.scan_sources(self.sources)
            if self.engine.stop_event.is_set():
                return

            if not self.engine.df.empty:
                unknown_devices = [d for d in self.engine.df['device'].unique() if brain.get_owner(d) == "Unknown"]
                if unknown_devices:
                    wait = threading.Event()

                    def on_save(mapping):
                        brain.set_owners_bulk(mapping)
                        wait.set()

                    self.root.after(0, lambda: DeviceTableWindow(self.root, unknown_devices, on_save))
                    wait.wait()

            self.engine.process_intelligence()
            if self.engine.stop_event.is_set():
                return
            self.engine.execute_organization(self.var_prim.get(), self.var_over.get())
            if self.engine.stop_event.is_set():
                return
            self.engine.generate_master_csv(brain_path)
            if self.engine.stop_event.is_set():
                return
            self.engine.generate_dashboard(brain_path)

            self.status("Completed successfully.")
            self.root.after(0, lambda: messagebox.showinfo("Done", "Organization complete."))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("Error", str(exc)))
        finally:
            self.root.after(0, lambda: self.start_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))


if __name__ == "__main__":
    app_root = tk.Tk()
    SherlockApp(app_root)
    app_root.mainloop()
