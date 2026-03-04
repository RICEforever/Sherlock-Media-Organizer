import os

# Suppress noisy OpenCV/FFmpeg stderr messages for corrupted video files
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"

import shutil
import time
import math
import threading
import hashlib
import json
import csv
import subprocess
import re
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from collections import Counter

# We will try to import PIL, geopy, imagehash and cv2, but provide helpful errors if missing
try:
    from PIL import Image, ExifTags
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
except ImportError:
    print("Error: Pillow library not found. Run 'pip install Pillow'")
try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut
except ImportError:
    print("Error: geopy library not found. Run 'pip install geopy'")
try:
    import imagehash
except ImportError:
    print("Error: imagehash library not found. Run 'pip install imagehash'")
try:
    import cv2
except ImportError:
    print("Error: opencv-python library not found. Run 'pip install opencv-python'")
try:
    import reverse_geocoder as rg
    HAS_RG = True
except ImportError:
    HAS_RG = False

# --- Utility Functions ---

class SuppressStderr:
    """Context manager to suppress stderr (including C-level writes like FFmpeg)."""
    def __enter__(self):
        try:
            self.err_fd = os.dup(2)
            self.devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(self.devnull, 2)
        except Exception:
            self.err_fd = None

    def __exit__(self, t, v, tb):
        if self.err_fd is not None:
            os.dup2(self.err_fd, 2)
            os.close(self.err_fd)
            os.close(self.devnull)

def calculate_visual_hash(file_path):
    """Calculates a visual perceptual hash (pHash) for images and video keyframes."""
    ext = os.path.splitext(file_path)[1].lower()
    image_exts = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp'}
    video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp'}

    try:
        if ext in image_exts:
            with Image.open(file_path) as img:
                return str(imagehash.phash(img))
        
        elif ext in video_exts:
            with SuppressStderr():
                cap = cv2.VideoCapture(file_path)
                if not cap.isOpened():
                    return None
                
                # Get frame at 10% of duration
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                if total_frames <= 0:
                    cap.release()
                    return None
                    
                target_frame = max(0, int(total_frames * 0.1))
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                
                ret, frame = cap.read()
                cap.release()
            
            if ret:
                # Convert OpenCV BGR to PIL RGB
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(frame_rgb)
                return str(imagehash.phash(pil_img))
    except Exception as e:
        print(f"Error generating visual hash for {file_path}: {e}")
    
    return None

def calculate_file_hash(file_path, block_size=65536):
    """Calculates SHA-256 hash of a file for robust duplicate detection."""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for block in iter(lambda: f.read(block_size), b''):
                sha256.update(block)
        return sha256.hexdigest()
    except Exception as e:
        print(f"Error hashing {file_path}: {e}")
        return None

def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance in KM between two points."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def get_exif_data(path):
    """Extracts Date, GPS info, and Device info for Images and Videos."""
    ext = Path(path).suffix.lower()
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    date_obj = mtime
    lat_lon = None
    device = "Unknown Device"
    
    if ext in {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp', '.heic'}:
        try:
            img = Image.open(path)
            exif = img._getexif()
            if exif:
                exif_data = {}
                for tag, value in exif.items():
                    decoded = ExifTags.TAGS.get(tag, tag)
                    exif_data[decoded] = value

                # 1. Date Extraction
                date_str = exif_data.get('DateTimeOriginal') or exif_data.get('DateTime')
                if date_str:
                    try:
                        date_obj = datetime.strptime(str(date_str).strip(), '%Y:%m:%d %H:%M:%S')
                    except: pass
                
                # 2. Device Extraction
                make = str(exif_data.get('Make', '')).strip()
                model = str(exif_data.get('Model', '')).strip()
                if make and model:
                    device = f"{make} {model}"
                else:
                    device = model or make or "Unknown Device"

                # 3. GPS Extraction
                gps_info = exif_data.get('GPSInfo')
                if gps_info:
                    def convert_to_degrees(value):
                        try:
                            d = float(value[0])
                            m = float(value[1])
                            s = float(value[2])
                            return d + (m / 60.0) + (s / 3600.0)
                        except: return 0.0
                    try:
                        lat = convert_to_degrees(gps_info[2])
                        if gps_info[1] != 'N': lat = -lat
                        lon = convert_to_degrees(gps_info[4])
                        if gps_info[3] != 'E': lon = -lon
                        if lat != 0 and lon != 0:
                            lat_lon = (lat, lon)
                    except: pass
        except Exception: pass
                
    elif ext in {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.cr2', '.arw', '.dng', '.3gp'}:
        try:
            # Filename date check
            date_match = re.search(r'(\d{4})[-_](\d{2})[-_](\d{2})', Path(path).name)
            if date_match:
                try:
                    date_obj = datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
                except: pass

            # FFprobe
            cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            out = subprocess.check_output(cmd, startupinfo=startupinfo, stderr=subprocess.STDOUT)
            data = json.loads(out)
            tags = data.get('format', {}).get('tags', {})
            
            # Date
            c_time = tags.get('creation_time')
            if c_time:
                try:
                    date_obj = datetime.strptime(c_time.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                except:
                    try:
                        date_obj = datetime.strptime(c_time.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    except: pass

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

            # GPS
            loc_str = tags.get('location') or tags.get('com.apple.quicktime.location.ISO6709')
            if loc_str:
                match = re.match(r'([+-][0-9.]+)([+-][0-9.]+)', loc_str)
                if match:
                    lat_lon = (float(match.group(1)), float(match.group(2)))
        except Exception: pass

    return date_obj, lat_lon, device

# --- Main App ---

class PhotoOrganizerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Sherlock Media Organizer")
        self.root.geometry("1000x950")
        
        # Apply a modern theme if available, otherwise use default ttk
        self.style = ttk.Style()
        self.style.theme_use('clam') # 'clam' is often the most modern-looking built-in theme
        
        # Custom styles for a cleaner look
        self.style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), foreground="#1a237e")
        self.style.configure("Subheader.TLabel", font=("Segoe UI", 10, "bold"))
        self.style.configure("Action.TButton", font=("Segoe UI", 10, "bold"))
        self.style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"), background="#2196F3", foreground="white")
        self.style.map("Accent.TButton", background=[('active', '#1976D2')])
        self.style.configure("Success.TButton", font=("Segoe UI", 12, "bold"), background="#4CAF50", foreground="white")
        self.style.map("Success.TButton", background=[('active', '#388E3C')])

        self.source_dirs = [] 
        self.dest_dir = tk.StringVar()
        self.dest_nickname = tk.StringVar(value="Main Drive")
        self.overflow_dir = tk.StringVar()
        self.overflow_nickname = tk.StringVar(value="Overflow Drive")
        self.reports_dir = tk.StringVar()
        self.geolocator = Nominatim(user_agent="photo_org_hotspot_v2")
        
        self.all_photos = [] 
        self.clusters = []   
        self.home_configs = {} 
        self.run_data = [] # To store details for reports
        
        self.cache_file = "media_metadata_cache.json"
        self.metadata_cache = self.load_cache()
        
        self.setup_gui()

    def load_cache(self):
        """Loads persistent cache of file hashes and metadata."""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading cache: {e}")
        return {}

    def save_cache(self):
        """Saves persistent cache to disk for future efficiency."""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.metadata_cache, f, indent=4)
        except Exception as e:
            print(f"Error saving cache: {e}")

    def setup_gui(self):
        # Main Container
        main_container = ttk.Frame(self.root, padding="20")
        main_container.pack(fill="both", expand=True)

        # 1. Folder Selection
        config_frame = ttk.LabelFrame(main_container, text=" 1. Configuration ", padding="15")
        config_frame.pack(fill="x", pady=(0, 15))
        
        # Source Folders
        ttk.Label(config_frame, text="Source Folders:", style="Subheader.TLabel").grid(row=0, column=0, sticky="nw", pady=5)
        
        list_frame = ttk.Frame(config_frame)
        list_frame.grid(row=0, column=1, padx=10, pady=5, sticky="nsew")
        
        self.src_listbox = tk.Listbox(list_frame, height=4, width=65, borderwidth=1, relief="flat", highlightthickness=1, highlightcolor="#2196F3", font=("Segoe UI", 9))
        self.src_listbox.pack(side="left", fill="both", expand=True)
        
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.src_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.src_listbox.config(yscrollcommand=scrollbar.set)
        
        src_btn_frame = ttk.Frame(config_frame)
        src_btn_frame.grid(row=0, column=2, sticky="n", pady=5)
        ttk.Button(src_btn_frame, text="Add Folder", width=15, style="Action.TButton", command=self.add_source_dir).pack(pady=2)
        ttk.Button(src_btn_frame, text="Remove", width=15, style="Action.TButton", command=self.remove_source_dir).pack(pady=2)
        
        # Dest Folders Grid
        dest_grid = ttk.Frame(config_frame)
        dest_grid.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        dest_grid.columnconfigure(1, weight=1)

        # Main Dest
        ttk.Label(dest_grid, text="Main Destination:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(dest_grid, textvariable=self.dest_dir).grid(row=0, column=1, padx=10, sticky="ew")
        
        dest_meta = ttk.Frame(dest_grid)
        dest_meta.grid(row=0, column=2, sticky="w")
        ttk.Label(dest_meta, text="Nickname:").pack(side="left")
        ttk.Entry(dest_meta, textvariable=self.dest_nickname, width=15).pack(side="left", padx=5)
        ttk.Button(dest_meta, text="Browse", width=10, command=lambda: self.dest_dir.set(filedialog.askdirectory())).pack(side="left")

        # Overflow
        ttk.Label(dest_grid, text="Overflow Folder:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(dest_grid, textvariable=self.overflow_dir).grid(row=1, column=1, padx=10, sticky="ew")
        
        over_meta = ttk.Frame(dest_grid)
        over_meta.grid(row=1, column=2, sticky="w")
        ttk.Label(over_meta, text="Nickname:").pack(side="left")
        ttk.Entry(over_meta, textvariable=self.overflow_nickname, width=15).pack(side="left", padx=5)
        ttk.Button(over_meta, text="Browse", width=10, command=lambda: self.overflow_dir.set(filedialog.askdirectory())).pack(side="left")

        # Reports
        ttk.Label(dest_grid, text="Reports Folder:").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(dest_grid, textvariable=self.reports_dir).grid(row=2, column=1, padx=10, sticky="ew")
        ttk.Button(dest_grid, text="Browse", width=10, command=lambda: self.reports_dir.set(filedialog.askdirectory())).grid(row=2, column=2, sticky="w")

        # 2. Analysis Button
        self.scan_btn = ttk.Button(main_container, text="🔍 1. Scan & Discover Hotspots", style="Accent.TButton", command=self.start_scan_thread)
        self.scan_btn.pack(pady=10)

        # 3. Hotspot Display
        self.hotspot_frame = ttk.LabelFrame(main_container, text=" 2. Review Identified Hotspots (Possible Homes) ", padding="15")
        self.hotspot_frame.pack(fill="both", expand=True, pady=10)
        
        canvas_container = ttk.Frame(self.hotspot_frame)
        canvas_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(canvas_container, highlightthickness=0, bg="#ffffff")
        scrollbar = ttk.Scrollbar(canvas_container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas, padding="10")

        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        # Make canvas window expand with canvas
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(canvas_window, width=e.width))

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 4. Final Action
        action_frame = ttk.Frame(main_container)
        action_frame.pack(fill="x", pady=10)

        self.organize_btn = ttk.Button(action_frame, text="🚀 2. Organize Everything", style="Success.TButton", state="disabled", command=self.start_organize_thread)
        self.organize_btn.pack(pady=5)

        self.progress = ttk.Progressbar(action_frame, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=5)
        
        # Custom log text using a monospaced font
        log_frame = ttk.Frame(main_container)
        log_frame.pack(fill="x", pady=5)
        
        self.log_text = tk.Text(log_frame, height=8, font=("Consolas", 9), state="disabled", bg="#f5f5f5", relief="flat", padx=10, pady=10)
        self.log_text.pack(fill="x")

    def add_source_dir(self):
        d = filedialog.askdirectory()
        if d and d not in self.source_dirs:
            self.source_dirs.append(d)
            self.src_listbox.insert(tk.END, d)

    def remove_source_dir(self):
        selected = self.src_listbox.curselection()
        if selected:
            idx = selected[0]
            d = self.src_listbox.get(idx)
            self.source_dirs.remove(d)
            self.src_listbox.delete(idx)

    def log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")
        self.root.update_idletasks()

    def start_scan_thread(self):
        if not self.source_dirs:
            messagebox.showerror("Error", "Add at least one source folder first")
            return
        self.scan_btn.config(state="disabled")
        threading.Thread(target=self.run_discovery, daemon=True).start()

    def run_discovery(self):
        # Support modern image and video formats from Android/iOS
        valid_exts = {
            '.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', # Images
            '.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp'      # Videos
        }
        files = []
        for src in self.source_dirs:
            for r, _, f in os.walk(src):
                for file in f:
                    if os.path.splitext(file)[1].lower() in valid_exts:
                        files.append(os.path.join(r, file))
        
        if not files:
            self.log("No photos or videos found.")
            self.scan_btn.config(state="normal")
            return

        self.log(f"Scanning {len(files)} files for visual similarity and binary identity...")
        self.all_photos = []
        binary_hashes = {} # sha256 -> path
        self.duplicate_paths = set() # Track for safe cleanup
        self.successfully_moved = set() # Track for safe cleanup
        duplicate_count = 0
        cache_hits = 0
        
        for i, path in enumerate(files):
            try:
                # 1. Calculate binary hash first - now used as the primary cache key
                # This makes the cache move-resilient (survives renames/moves)
                f_hash = calculate_file_hash(path)
                f_size = os.path.getsize(path)
                
                # Check persistent cache
                cached_data = self.metadata_cache.get(f_hash)
                
                if cached_data:
                    v_hash = cached_data['p_hash']
                    dt = datetime.fromisoformat(cached_data['date']) if cached_data.get('date') else None
                    coords = tuple(cached_data['coords']) if cached_data.get('coords') else None
                    device = cached_data.get('device', 'Unknown Device')
                    cache_hits += 1
                else:
                    # 2. Calculate Visual Hash (expensive, especially for videos)
                    v_hash = calculate_visual_hash(path)
                        
                    # 3. Extract Metadata
                    dt, coords, device = get_exif_data(path)
                    
                    # Update cache using f_hash as key
                    self.metadata_cache[f_hash] = {
                        'p_hash': v_hash,
                        'date': dt.isoformat() if dt else None,
                        'coords': list(coords) if coords else None,
                        'device': device,
                        'last_seen_path': path
                    }

                # Binary Duplicate Check (skip if we already have this file in this run)
                if f_hash in binary_hashes:
                    duplicate_count += 1
                    self.duplicate_paths.add(path)
                    continue
                binary_hashes[f_hash] = path
                
                record = {
                    'path': path, 
                    'date': dt, 
                    'coords': coords, 
                    'device': device,
                    'cluster_id': None,
                    'is_home': False,
                    'p_hash': v_hash,
                    'f_hash': f_hash,
                    'size': f_size
                }
                
                self.all_photos.append(record)
            except Exception as e:
                self.log(f"Error processing {path}: {e}")
            
            if i % 10 == 0:
                self.progress['value'] = (i / len(files)) * 100
                self.root.update_idletasks()

        if cache_hits > 0:
            self.log(f"Efficiency Boost: Loaded {cache_hits} files from persistent cache.")
        if duplicate_count > 0:
            self.log(f"Found {duplicate_count} exact binary duplicates (will be skipped).")
        
        self.save_cache() # Save updated cache after scan
        
        # Group by device to show user
        devices = Counter([p['device'] for p in self.all_photos])
        self.log(f"Found {len(devices)} unique source devices.")
        for d, count in devices.items():
            self.log(f"  - {d}: {count} photos")

        # Clustering Logic (Hotspots)
        clusters = [] 
        gps_records = [r for r in self.all_photos if r['coords']]
        self.log(f"Found GPS in {len(gps_records)} photos. Identifying clusters...")
        
        for rec in gps_records:
            lat, lon = rec['coords']
            matched = False
            for c in clusters:
                dist = haversine(lat, lon, c['center'][0], c['center'][1])
                if dist < 1.0: # 1km radius
                    c['records'].append(rec)
                    c['count'] += 1
                    matched = True
                    break
            if not matched:
                clusters.append({'center': (lat, lon), 'count': 1, 'records': [rec]})

        clusters.sort(key=lambda x: x['count'], reverse=True)
        
        # Resolve names in background thread
        self.log(f"Resolving names for {len(clusters)} hotspots...")
        for i, c in enumerate(clusters):
            try:
                time.sleep(1.1) # Respect Nominatim rate limit (1 req/sec)
                loc = self.geolocator.reverse(c['center'], exactly_one=True, timeout=5)
                if loc:
                    addr = loc.raw.get('address', {})
                    country = addr.get('country', '').lower()
                    
                    if country == 'india':
                        # Priority: District -> County -> City
                        loc_name = addr.get('state_district') or addr.get('county') or addr.get('city') or "Unknown District"
                    else:
                        # Priority: City -> Town -> Village
                        loc_name = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('municipality') or "Unknown City"
                    
                    c['resolved_name'] = loc_name
                elif HAS_RG:
                    # Offline backup using reverse_geocoder
                    results = rg.search([c['center']], verbose=False)
                    if results:
                        res = results[0]
                        # For India, try to use admin2 (often district)
                        if res.get('cc') == 'IN':
                            loc_name = res.get('admin2') or res.get('admin1') or res.get('name')
                        else:
                            loc_name = res.get('name') or res.get('admin1')
                        c['resolved_name'] = f"{loc_name} (Approx)"
                    else:
                        c['resolved_name'] = f"Location_{round(c['center'][0],2)}_{round(c['center'][1],2)}"
                else:
                    c['resolved_name'] = f"Location_{round(c['center'][0],2)}_{round(c['center'][1],2)}"
            except Exception as e:
                self.log(f"Resolution error for cluster {i}: {e}")
                c['resolved_name'] = f"Location_{round(c['center'][0],2)}_{round(c['center'][1],2)}"
        
        self.clusters = clusters
        self.root.after(0, self.display_hotspots)

    def display_hotspots(self):
        # Clear existing
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        self.log("Discovery complete. Please designate 'Home' locations in the list above.")
        
        # Table Headers
        headers = ["Identified Location", "Media Count", "Mark as Home?", "Custom Folder Name"]
        for col, text in enumerate(headers):
            lbl = ttk.Label(self.scrollable_frame, text=text, font=("Segoe UI", 10, "bold"))
            lbl.grid(row=0, column=col, padx=10, pady=(0, 10), sticky="w")

        # Table Content
        for i, c in enumerate(self.clusters):
            loc_name = c.get('resolved_name', "Unknown")
            
            # Use alternating row colors for better readability (simulated with Frame)
            row_bg = "#ffffff" if i % 2 == 0 else "#f9f9f9"
            row_frame = tk.Frame(self.scrollable_frame, bg=row_bg)
            row_frame.grid(row=i+1, column=0, columnspan=4, sticky="ew")
            self.scrollable_frame.columnconfigure(3, weight=1)

            ttk.Label(self.scrollable_frame, text=loc_name, background=row_bg).grid(row=i+1, column=0, padx=10, pady=5, sticky="w")
            ttk.Label(self.scrollable_frame, text=f"{c['count']} files", background=row_bg).grid(row=i+1, column=1, padx=10, pady=5, sticky="w")
            
            is_home_var = tk.BooleanVar()
            name_var = tk.StringVar(value=loc_name)
            
            cb = ttk.Checkbutton(self.scrollable_frame, variable=is_home_var)
            cb.grid(row=i+1, column=2, padx=10, pady=5)
            
            ent = ttk.Entry(self.scrollable_frame, textvariable=name_var, width=30)
            ent.grid(row=i+1, column=3, padx=10, pady=5, sticky="ew")
            
            # Store references
            c['ui_home_var'] = is_home_var
            c['ui_name_var'] = name_var

        self.organize_btn.config(state="normal")
        self.progress['value'] = 100

    def start_organize_thread(self):
        if not self.dest_dir.get():
            messagebox.showerror("Error", "Select destination folder")
            return
        self.organize_btn.config(state="disabled")
        threading.Thread(target=self.run_organization, daemon=True).start()

    def resolve_unknowns(self):
        """Attempts to assign locations to no-GPS photos using same-device proximity and sequence analysis."""
        self.log("Reducing unknowns using device proximity and sequence analysis...")
        
        # Sort all photos by device then date for sequence analysis
        by_device = {}
        for r in self.all_photos:
            d = r['device']
            if d not in by_device: by_device[d] = []
            by_device[d].append(r)
            
        resolved_count = 0
        for device, photos in by_device.items():
            photos.sort(key=lambda x: x['date'])
            
            # Step 1: Sequence Interpolation
            # If A(GPS) ... B(No-GPS) ... C(GPS) and A,C are near each other, B is at that location
            for i in range(len(photos)):
                if photos[i].get('coords'): continue
                
                # Find nearest GPS before and after
                before = None
                for j in range(i-1, -1, -1):
                    if photos[j].get('coords'):
                        before = photos[j]
                        break
                
                after = None
                for j in range(i+1, len(photos)):
                    if photos[j].get('coords'):
                        after = photos[j]
                        break
                
                # Logic: If both exist and are < 12hrs apart
                if before and after:
                    time_gap = after['date'] - before['date']
                    if time_gap < timedelta(hours=12):
                        # If they are at the same hotspot (within 1km), assign that location
                        dist = haversine(before['coords'][0], before['coords'][1], after['coords'][0], after['coords'][1])
                        if dist < 1.0:
                            photos[i]['coords'] = before['coords']
                            photos[i]['inferred'] = True
                            resolved_count += 1
                            continue
                
                # Step 2: Closest-neighbor Fallback (12hr window)
                best_neighbor = None
                min_diff = timedelta(hours=12)
                
                if before and (photos[i]['date'] - before['date']) < min_diff:
                    best_neighbor = before
                    min_diff = photos[i]['date'] - before['date']
                
                if after and (after['date'] - photos[i]['date']) < min_diff:
                    best_neighbor = after
                    min_diff = after['date'] - photos[i]['date']
                    
                if best_neighbor:
                    photos[i]['coords'] = best_neighbor['coords']
                    photos[i]['inferred'] = True
                    resolved_count += 1

        self.log(f"Successfully resolved locations for {resolved_count} photos using device/sequence context.")

    def run_organization(self):
        dst_main = self.dest_dir.get()
        overflow = self.overflow_dir.get()
        reports_path = self.reports_dir.get()

        if not dst_main:
            messagebox.showerror("Error", "Select main destination folder")
            return
        if not reports_path:
            messagebox.showerror("Error", "Select a folder for reports and run data")
            return
            
        self.log("Starting organization and cleanup...")
        self.run_data = [] # Reset run data

        # 0. Resolve unknowns
        self.resolve_unknowns()

        # 1. Update clusters and assign relative folders
        for c in self.clusters:
            if c['ui_home_var'].get():
                c['custom_name'] = c['ui_name_var'].get()
                
        for rec in self.all_photos:
            if rec['coords']:
                lat, lon = rec['coords']
                assigned = False
                for c in self.clusters:
                    dist = haversine(lat, lon, c['center'][0], c['center'][1])
                    if dist < 1.0:
                        rec['rel_folder'] = c['custom_name'] if c['ui_home_var'].get() else c['resolved_name']
                        rec['is_home'] = c['ui_home_var'].get()
                        rec['cluster_ref'] = c
                        assigned = True
                        break

        # 2. Process Trip Groups (Non-home)
        non_home_photos = [r for r in self.all_photos if r.get('coords') and not r.get('is_home')]
        by_loc = {}
        for r in non_home_photos:
            loc = r['cluster_ref']['resolved_name']
            if loc not in by_loc: by_loc[loc] = []
            by_loc[loc].append(r)
            
        for loc, recs in by_loc.items():
            recs.sort(key=lambda x: x['date'])
            current_trip = [recs[0]]
            for i in range(1, len(recs)):
                if (recs[i]['date'] - recs[i-1]['date']).days > 5:
                    self.finalize_trip(current_trip, loc)
                    current_trip = [recs[i]]
                else:
                    current_trip.append(recs[i])
            self.finalize_trip(current_trip, loc)

        # 3. Process remaining No-GPS photos
        still_no_gps = [r for r in self.all_photos if not r.get('coords')]
        for rec in still_no_gps:
            month_folder = rec['date'].strftime('%Y-%m')
            rec['rel_folder'] = os.path.join("Unsorted_Remaining", month_folder)

        # 4. Execute file MOVES and handle duplicates
        total = len(self.all_photos)
        moved_count = 0
        p_hash_registry = {} # p_hash -> target_path
        
        for i, rec in enumerate(self.all_photos):
            # Dynamic Disk Check
            active_dst = dst_main
            active_nickname = self.dest_nickname.get()
            
            free_bytes = shutil.disk_usage(dst_main).free
            if free_bytes < (2 * 1024 * 1024 * 1024): # 2GB
                if overflow and os.path.exists(overflow):
                    active_dst = overflow
                    active_nickname = self.overflow_nickname.get()
                else:
                    self.log("CRITICAL: Main destination full and no overflow set! Stopping.")
                    break
            
            target_base = rec.get('rel_folder', "Miscellaneous")
            
            # Use Potential_Duplicates folder if p_hash already seen
            is_visual_dup = False
            if rec['p_hash'] and rec['p_hash'] in p_hash_registry:
                is_visual_dup = True
                target = os.path.join(active_dst, "Potential_Duplicates", target_base)
            else:
                target = os.path.join(active_dst, target_base)
                if rec['p_hash']:
                    p_hash_registry[rec['p_hash']] = True
            
            os.makedirs(target, exist_ok=True)
            
            fname = os.path.basename(rec['path'])
            dest_path = os.path.join(target, fname)
            
            status = "Moved (Visual Duplicate)" if is_visual_dup else "Moved"
            final_dest = dest_path

            # Collision check (file name already exists)
            count = 1
            while os.path.exists(dest_path):
                name, ext = os.path.splitext(fname)
                dest_path = os.path.join(target, f"{name}_{count}{ext}")
                count += 1
            
            if dest_path:
                try:
                    # Capture original path for safety tracking
                    original_src = rec['path']
                    shutil.move(original_src, dest_path)
                    self.successfully_moved.add(original_src)
                    final_dest = dest_path
                    moved_count += 1
                except Exception as e:
                    self.log(f"Error moving {fname}: {e}")
                    status = f"Error: {e}"
            
            self.run_data.append({
                'original_path': rec['path'],
                'final_destination': final_dest,
                'location_tag': target_base,
                'drive_nickname': active_nickname,
                'status': status,
                'p_hash': rec['p_hash'],
                'f_hash': rec['f_hash'],
                'size_bytes': rec['size'],
                'date': str(rec['date']),
                'device': rec['device']
            })
            
            if i % 10 == 0:
                self.progress['value'] = (i / total) * 100
                self.root.update_idletasks()

        # 5. Cleanup and Reports
        self.log("Finalizing duplicate cleanup in source folders...")
        self.remove_all_duplicates_and_empty_dirs()
        self.generate_reports(reports_path)

        self.log(f"✅ DONE! Moved {moved_count} unique files. Reports generated.")
        messagebox.showinfo("Success", f"Organization complete. Reports saved in {reports_path}")

    def generate_reports(self, output_dir):
        """Generates JSON, CSV, and HTML reports for the run."""
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 1. JSON Report
        json_path = os.path.join(output_dir, f"run_report_{timestamp}.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(self.run_data, f, indent=4)

        # 2. CSV Report
        csv_path = os.path.join(output_dir, f"run_report_{timestamp}.csv")
        if self.run_data:
            keys = self.run_data[0].keys()
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(self.run_data)

        # 3. HTML Report
        html_path = os.path.join(output_dir, f"run_report_{timestamp}.html")
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(f"<html><head><title>Run Report {timestamp}</title>")
            f.write("<style>table { border-collapse: collapse; width: 100%; } th, td { border: 1px solid #ddd; padding: 8px; text-align: left; } tr:nth-child(even){background-color: #f2f2f2} th { background-color: #4CAF50; color: white; }</style>")
            f.write(f"</head><body><h1>Organization Run Report: {timestamp}</h1>")
            f.write("<table><tr><th>Status</th><th>Original Path</th><th>Final Destination</th><th>Device</th><th>Size</th></tr>")
            for item in self.run_data:
                f.write(f"<tr><td>{item['status']}</td><td>{item['original_path']}</td><td>{item['final_destination']}</td><td>{item['device']}</td><td>{round(item['size_bytes']/1024, 2)} KB</td></tr>")
            f.write("</table></body></html>")

    def remove_all_duplicates_and_empty_dirs(self):
        """Final sweep to remove binary duplicates and delete empty source folders."""
        self.log(f"Cleaning up {len(self.duplicate_paths)} confirmed duplicates from source...")
        
        # 1. Remove binary duplicates explicitly
        for fpath in self.duplicate_paths:
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception as e:
                self.log(f"Warning: Could not remove duplicate {os.path.basename(fpath)}: {e}")

        # 2. Walk bottom-up to remove empty folders only
        for src in self.source_dirs:
            if not os.path.exists(src): continue
            for r, dirs, files in os.walk(src, topdown=False):
                try:
                    if not os.listdir(r):
                        os.rmdir(r)
                except: pass

    def finalize_trip(self, trip_records, loc_name):
        start = trip_records[0]['date'].strftime('%Y-%m-%d')
        end = trip_records[-1]['date'].strftime('%Y-%m-%d')
        folder_name = f"Trip_{start}_to_{end}" if start != end else f"Trip_{start}"
        
        target_rel = os.path.join(loc_name, folder_name)
        for r in trip_records:
            r['rel_folder'] = target_rel

if __name__ == "__main__":
    root = tk.Tk()
    app = PhotoOrganizerApp(root)
    root.mainloop()
