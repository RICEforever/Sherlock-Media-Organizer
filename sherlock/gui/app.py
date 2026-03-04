import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
from pathlib import Path
import queue
import webbrowser
import json
import pandas as pd
import logging
from datetime import datetime

from ..core.database import SherlockDB
from ..features.scanner import MediaScanner
from ..features.intelligence import TripIntelligence
from ..features.dashboard import DashboardGenerator
from ..features.organizer import MediaOrganizer

logger = logging.getLogger(__name__)

class DeviceTableWindow(tk.Toplevel):
    def __init__(self, parent, devices, existing_mapping, callback):
        super().__init__(parent)
        self.title("Map Devices to Owners & Nicknames")
        self.geometry("700x450")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.owner_entries = {}
        self.nickname_entries = {}

        tk.Label(self, text="Assign owners and nicknames to devices", font=("Segoe UI", 11, "bold")).pack(pady=10)
        
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10)
        ttk.Label(header, text="Device Model", font=("Segoe UI", 9, "bold"), width=30).pack(side=tk.LEFT)
        ttk.Label(header, text="Owner Name", font=("Segoe UI", 9, "bold"), width=20).pack(side=tk.LEFT, padx=5)
        ttk.Label(header, text="Device Nickname", font=("Segoe UI", 9, "bold"), width=20).pack(side=tk.LEFT)

        # Scrollable frame for many devices
        canvas = tk.Canvas(self)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=10)
        scrollbar.pack(side="right", fill="y")

        for dev in sorted(devices):
            row = ttk.Frame(scrollable_frame)
            row.pack(fill=tk.X, pady=4, padx=5)
            
            ttk.Label(row, text=dev, width=30).pack(side=tk.LEFT)
            
            owner_entry = ttk.Entry(row, width=20)
            existing_data = existing_mapping.get(dev, {})
            if isinstance(existing_data, str):
                owner_entry.insert(0, existing_data)
                nickname = ""
            else:
                owner_entry.insert(0, existing_data.get('owner', ""))
                nickname = existing_data.get('nickname', "")

            owner_entry.pack(side=tk.LEFT, padx=5)
            self.owner_entries[dev] = owner_entry
            
            nick_entry = ttk.Entry(row, width=20)
            nick_entry.insert(0, nickname)
            nick_entry.pack(side=tk.LEFT)
            self.nickname_entries[dev] = nick_entry

        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill=tk.X, padx=20, pady=12)
        ttk.Button(btn_bar, text="Save & Continue", command=self._save).pack(side=tk.RIGHT)

    def _save(self):
        mapping = {}
        for dev in self.owner_entries:
            owner = self.owner_entries[dev].get().strip()
            nickname = self.nickname_entries[dev].get().strip()
            if owner or nickname:
                mapping[dev] = {'owner': owner, 'nickname': nickname}
        self.callback(mapping)
        self.destroy()

class HomeTableWindow(tk.Toplevel):
    def __init__(self, parent, locations, existing_mapping, callback):
        super().__init__(parent)
        self.title("Map Home Locations to Nicknames")
        self.geometry("700x450")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.nickname_entries = {}

        tk.Label(self, text="Assign 'Home' nicknames (e.g., 'Home', 'Office') to locations.\nLeave blank if not a home location.", font=("Segoe UI", 11, "bold")).pack(pady=10)
        
        # Header
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=10)
        ttk.Label(header, text="Extracted Location", font=("Segoe UI", 9, "bold"), width=50).pack(side=tk.LEFT)
        ttk.Label(header, text="Home Nickname", font=("Segoe UI", 9, "bold"), width=30).pack(side=tk.LEFT, padx=5)

        # Scrollable frame for many locations
        canvas = tk.Canvas(self)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=10)
        scrollbar.pack(side="right", fill="y")

        for loc in sorted(locations):
            row = ttk.Frame(scrollable_frame)
            row.pack(fill=tk.X, pady=4, padx=5)
            
            ttk.Label(row, text=loc, width=50).pack(side=tk.LEFT)
            
            nick_entry = ttk.Entry(row, width=30)
            existing_data = existing_mapping.get(loc, "")
            nick_entry.insert(0, existing_data)
            nick_entry.pack(side=tk.LEFT, padx=5)
            self.nickname_entries[loc] = nick_entry

        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill=tk.X, padx=20, pady=12)
        ttk.Button(btn_bar, text="Save & Continue", command=self._save).pack(side=tk.RIGHT)

    def _save(self):
        mapping = {}
        for loc in self.nickname_entries:
            nickname = self.nickname_entries[loc].get().strip()
            # We save the mapping even if blank, so we don't prompt again for this location
            mapping[loc] = nickname
        self.callback(mapping)
        self.destroy()

class SherlockApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Sherlock Media Organiser v2.0 (Modular)")
        self.root.geometry("1000x850")
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)
        
        # Data
        self.sources = []
        self.db = None
        self.scanner = MediaScanner(status_cb=self._update_status, progress_cb=self._update_progress)
        self.intelligence = TripIntelligence({
            'trip_gap_hours': "72",
            'trip_distance_km': "100",
            'home_cities': []
        })
        self.home_cities = [] 

        self._setup_ui()
        
    def _setup_ui(self):
        # Notebook for Tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.tab_config = ttk.Frame(self.notebook)
        self.tab_results = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_config, text="Configuration")
        self.notebook.add(self.tab_results, text="Trips & Results")

        # --- TAB 1: CONFIGURATION ---
        # 1. Workspace Configuration (Where DB and Reports live)
        frame_config = ttk.LabelFrame(self.tab_config, text="Workspace (DB & Reports)", padding=10)
        frame_config.pack(fill=tk.X, padx=10, pady=5)
        
        self.var_db_path = tk.StringVar()
        ttk.Entry(frame_config, textvariable=self.var_db_path, width=60).grid(row=0, column=1, padx=5)
        ttk.Button(frame_config, text="Browse", command=self._browse_db).grid(row=0, column=2)
        ttk.Label(frame_config, text="Work Dir:").grid(row=0, column=0, sticky=tk.W)

        # 2. Source Folders & Home Cities
        frame_src_home = ttk.Frame(self.tab_config)
        frame_src_home.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Sources
        frame_src = ttk.LabelFrame(frame_src_home, text="Source Folders", padding=10)
        frame_src.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.list_src = tk.Listbox(frame_src, height=4)
        self.list_src.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        btn_frame_src = ttk.Frame(frame_src)
        btn_frame_src.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(btn_frame_src, text="+", width=3, command=self._add_source).pack(pady=2)
        ttk.Button(btn_frame_src, text="-", width=3, command=self._remove_source).pack(pady=2)

        # 3. Organization Targets
        frame_dest = ttk.LabelFrame(self.tab_config, text="Organization Settings", padding=10)
        frame_dest.pack(fill=tk.X, padx=10, pady=5)

        # Primary Destination
        ttk.Label(frame_dest, text="Primary Destination:").grid(row=0, column=0, sticky=tk.W)
        self.var_dest_path = tk.StringVar()
        ttk.Entry(frame_dest, textvariable=self.var_dest_path, width=60).grid(row=0, column=1, padx=5)
        ttk.Button(frame_dest, text="Browse", command=lambda: self._browse_dir(self.var_dest_path)).grid(row=0, column=2)

        # Overflow Destination
        ttk.Label(frame_dest, text="Overflow Destination:").grid(row=1, column=0, sticky=tk.W)
        self.var_overflow_path = tk.StringVar()
        ttk.Entry(frame_dest, textvariable=self.var_overflow_path, width=60).grid(row=1, column=1, padx=5)
        ttk.Button(frame_dest, text="Browse", command=lambda: self._browse_dir(self.var_overflow_path)).grid(row=1, column=2)

        # External Drive Info
        drive_frame = ttk.Frame(frame_dest)
        drive_frame.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=5)
        
        self.var_is_external = tk.BooleanVar()
        ttk.Checkbutton(drive_frame, text="Is External Drive / Portable Device", variable=self.var_is_external).pack(side=tk.LEFT)
        
        ttk.Label(drive_frame, text="  Device Nickname:").pack(side=tk.LEFT)
        self.var_device_nickname = tk.StringVar()
        ttk.Entry(drive_frame, textvariable=self.var_device_nickname, width=20).pack(side=tk.LEFT, padx=5)

        # 4. Advanced Settings
        frame_advanced = ttk.LabelFrame(self.tab_config, text="Advanced Settings", padding=10)
        frame_advanced.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(frame_advanced, text="Trip Gap (Hours):").grid(row=0, column=0, sticky=tk.W)
        self.var_gap_hours = tk.StringVar(value="72")
        ttk.Entry(frame_advanced, textvariable=self.var_gap_hours, width=10).grid(row=0, column=1, padx=5, sticky=tk.W)
        
        ttk.Label(frame_advanced, text="Trip Distance (KM):").grid(row=0, column=2, sticky=tk.W, padx=(20,0))
        self.var_distance_km = tk.StringVar(value="100")
        ttk.Entry(frame_advanced, textvariable=self.var_distance_km, width=10).grid(row=0, column=3, padx=5, sticky=tk.W)

        ttk.Label(frame_advanced, text="Default Owner:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.var_owner = tk.StringVar(value="Unknown")
        ttk.Entry(frame_advanced, textvariable=self.var_owner, width=20).grid(row=1, column=1, padx=5, sticky=tk.W, pady=5)

        self.var_delete_source = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame_advanced, text="Move files (Delete from source)", variable=self.var_delete_source).grid(row=1, column=2, columnspan=2, sticky=tk.W, padx=(20,0))
        
        ttk.Label(frame_advanced, text="Organization Mode:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.var_org_mode = tk.StringVar(value="Location")
        ttk.Combobox(frame_advanced, textvariable=self.var_org_mode, values=["Location", "Trip"], state="readonly", width=17).grid(row=2, column=1, padx=5, sticky=tk.W, pady=5)

        # --- TAB 2: RESULTS ---
        frame_results_top = ttk.Frame(self.tab_results, padding=10)
        frame_results_top.pack(fill=tk.X)
        ttk.Label(frame_results_top, text="Identified Trips", font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        
        self.tree_trips = ttk.Treeview(self.tab_results, columns=("Trip", "Date", "Files", "Device", "Participants"), show='headings')
        self.tree_trips.heading("Trip", text="Trip Name")
        self.tree_trips.heading("Date", text="Start Date")
        self.tree_trips.heading("Files", text="File Count")
        self.tree_trips.heading("Device", text="Primary Device")
        self.tree_trips.heading("Participants", text="Participants")
        
        self.tree_trips.column("Trip", width=200)
        self.tree_trips.column("Date", width=100)
        self.tree_trips.column("Files", width=80)
        self.tree_trips.column("Device", width=150)
        
        self.tree_trips.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Actions (Shared at bottom)
        frame_actions = ttk.Frame(self.root, padding=10)
        frame_actions.pack(fill=tk.X)
        
        self.btn_scan = ttk.Button(frame_actions, text="Start Scan & Organize", command=self._start_scan)
        self.btn_scan.pack(side=tk.LEFT, padx=5)
        
        self.btn_stop = ttk.Button(frame_actions, text="Stop", command=self._stop_scan, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)
        
        self.btn_ai = ttk.Button(frame_actions, text="Export AI JSON", command=self._export_ai, state=tk.DISABLED)
        self.btn_ai.pack(side=tk.LEFT, padx=5)

        self.btn_csv = ttk.Button(frame_actions, text="Export Master CSV", command=self._export_csv, state=tk.DISABLED)
        self.btn_csv.pack(side=tk.LEFT, padx=5)
        
        self.btn_dash = ttk.Button(frame_actions, text="Generate Dashboard", command=self._generate_dashboard, state=tk.DISABLED)
        self.btn_dash.pack(side=tk.LEFT, padx=5)

        # Status
        self.lbl_status = ttk.Label(self.root, text="Ready")
        self.lbl_status.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)
        
        self.progress = ttk.Progressbar(self.root, orient=tk.HORIZONTAL, mode='determinate')
        self.progress.pack(side=tk.BOTTOM, fill=tk.X, padx=10)

        # Auto-load last work dir
        self._auto_load_work_dir()

    def _auto_load_work_dir(self):
        pref_file = Path(".sherlock_pref.json")
        if pref_file.exists():
            try:
                with open(pref_file, 'r') as f:
                    prefs = json.load(f)
                    path = prefs.get('last_work_dir')
                    if path and Path(path).exists():
                        self.var_db_path.set(path)
                        self._initialize_db(path)
            except: pass

    def _browse_db(self):
        path = filedialog.askdirectory()
        if path:
            self.var_db_path.set(path)
            self._initialize_db(path)
            # Save preference
            with open(".sherlock_pref.json", 'w') as f:
                json.dump({'last_work_dir': path}, f)

    def _initialize_db(self, path):
        self.db = SherlockDB(Path(path))
        
        # Load config from DB
        gap = self.db.get_config('trip_gap_hours', "72")
        dist = self.db.get_config('trip_distance_km', "100")
        owner = self.db.get_config('default_owner', "Unknown")
        dest = self.db.get_config('primary_destination', "")
        overflow = self.db.get_config('overflow_destination', "")
        org_mode = self.db.get_config('org_mode', "Location")

        self.var_gap_hours.set(gap)
        self.var_distance_km.set(dist)
        self.var_owner.set(owner)
        self.var_dest_path.set(dest)
        self.var_overflow_path.set(overflow)
        self.var_org_mode.set(org_mode)

        hc_raw = self.db.get_config('home_cities', "[]")
        try:
            self.home_cities = json.loads(hc_raw)
            self.list_home.delete(0, tk.END)
            for h in self.home_cities:
                self.list_home.insert(tk.END, f"{h['name']} ({h['lat']}, {h['lon']})")
        except: self.home_cities = []

        self.intelligence.update_config({
            'trip_gap_hours': gap,
            'trip_distance_km': dist,
            'home_cities': self.home_cities
        })
        self.btn_ai.config(state=tk.NORMAL)
        self.btn_csv.config(state=tk.NORMAL)
        self.btn_dash.config(state=tk.NORMAL)
        self.lbl_status.config(text=f"Database initialized at {path}")
        self._refresh_trips_tree()

    def _refresh_trips_tree(self):
        if not self.db: return
        for item in self.tree_trips.get_children():
            self.tree_trips.delete(item)
        
        trips = self.db.get_all_trips()
        for t in trips:
            self.tree_trips.insert("", tk.END, values=(
                t.name, 
                t.start_date.strftime('%Y-%m-%d'), 
                t.file_count, 
                t.primary_device, 
                ", ".join(t.participants)
            ))

    def _browse_dir(self, var):
        path = filedialog.askdirectory()
        if path: var.set(path)

    def _add_source(self):
        path = filedialog.askdirectory()
        if path and path not in self.sources:
            self.sources.append(path)
            self.list_src.insert(tk.END, path)

    def _remove_source(self):
        sel = self.list_src.curselection()
        for index in reversed(sel):
            self.sources.pop(index)
            self.list_src.delete(index)

    def _update_status(self, msg):
        self.root.after(0, lambda: self.lbl_status.config(text=msg))

    def _update_progress(self, val, total):
        self.root.after(0, lambda: self.progress.config(maximum=total, value=val))

    def _request_overflow_path(self):
        event = threading.Event()
        answer = {'path': None}

        def _prompt():
            msg = "Primary and overflow destinations are full. Would you like to add another overflow folder?"
            if messagebox.askyesno("Storage Full", msg):
                answer['path'] = filedialog.askdirectory(title="Select New Overflow Folder")
            event.set()

        self.root.after(0, _prompt)
        event.wait()
        return answer['path']

    def _stop_scan(self):
        if self.scanner:
            self.scanner.request_stop()
            self.lbl_status.config(text="Stopping...")

    def _start_scan(self):
        if not self.db or not self.sources:
            messagebox.showerror("Error", "Check Work Dir and Source Folders.")
            return

        self.db.save_config('trip_gap_hours', self.var_gap_hours.get())
        self.db.save_config('trip_distance_km', self.var_distance_km.get())
        self.db.save_config('default_owner', self.var_owner.get())
        self.db.save_config('org_mode', self.var_org_mode.get())
        self.db.save_config('home_cities', json.dumps(self.home_cities))
        self.db.save_config('primary_destination', self.var_dest_path.get())
        self.db.save_config('overflow_destination', self.var_overflow_path.get())

        threading.Thread(target=self._run_process, daemon=True).start()

    def _run_process(self):
        if not self.var_dest_path.get():
            self.root.after(0, lambda: messagebox.showerror("Error", "Select Primary Destination."))
            return

        self.btn_scan.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.scanner.stop_requested = False
        try:
            self.intelligence.update_config({
                'trip_gap_hours': self.var_gap_hours.get(),
                'trip_distance_km': self.var_distance_km.get(),
                'home_cities': self.home_cities
            })

            self._update_status("Scanning...")
            scanned = self.scanner.scan(self.sources, db=self.db) 
            if self.scanner.stop_requested: return

            # Batch save initial scan results to DB
            self.db.save_media_batch(scanned)

            # Owner Mapping Logic
            owner_mapping = self.db.get_owner_mapping()
            unique_devices = list(set(f.device for f in scanned if f.device))
            
            unknown_devices = [d for d in unique_devices if d not in owner_mapping]
            
            if unknown_devices:
                wait_event = threading.Event()
                def on_mapping_saved(new_mapping):
                    owner_mapping.update(new_mapping)
                    self.db.save_owner_mapping(owner_mapping)
                    wait_event.set()
                
                self.root.after(0, lambda: DeviceTableWindow(self.root, unique_devices, owner_mapping, on_mapping_saved))
                self._update_status("Waiting for owner mapping...")
                wait_event.wait()

            for f in scanned:
                data = owner_mapping.get(f.device, {})
                if isinstance(data, str):
                    f.owner = data
                    f.device_nickname = f.device
                else:
                    f.owner = data.get('owner', self.var_owner.get())
                    f.device_nickname = data.get('nickname', f.device)

            self._update_status("Geocoding media...")
            self.intelligence.geocode_media(scanned)
            if self.scanner.stop_requested: return

            # Home Mapping Logic
            home_mapping = self.db.get_home_mapping()
            unique_locations = list(set(f.location for f in scanned if f.location and f.location != "Unknown"))
            unknown_locations = [loc for loc in unique_locations if loc not in home_mapping]

            if unknown_locations:
                wait_event2 = threading.Event()
                def on_home_mapping_saved(new_mapping):
                    home_mapping.update(new_mapping)
                    self.db.save_home_mapping(home_mapping)
                    wait_event2.set()
                
                self.root.after(0, lambda: HomeTableWindow(self.root, unknown_locations, home_mapping, on_home_mapping_saved))
                self._update_status("Waiting for home mapping...")
                wait_event2.wait()

            # Build effective home mapping by filtering out blanks
            effective_home_mapping = {k: v for k, v in home_mapping.items() if v}

            self._update_status("Analyzing...")
            trip_groups = self.intelligence.process_trips(scanned, effective_home_mapping)
            if self.scanner.stop_requested: return

            self._update_status("Organizing...")
            organizer = MediaOrganizer(
                dest_path=self.var_dest_path.get(),
                overflow_path=self.var_overflow_path.get(),
                request_overflow_cb=self._request_overflow_path
            )
            organized = organizer.organize(
                trip_groups, 
                self._update_status, 
                self.var_delete_source.get(),
                org_mode=self.var_org_mode.get(),
                home_mapping=effective_home_mapping
            )
            if self.scanner.stop_requested: return

            self._update_status("Saving to DB...")
            all_organized = []
            for trip, group_files in trip_groups:
                trip_id = self.db.save_trip(trip)
                for f in group_files:
                    f.trip_id = trip_id
                all_organized.extend(group_files)
            
            self.db.save_media_batch(all_organized)
            
            self._cleanup_workspace()
            self.root.after(0, self._refresh_trips_tree)
            self.root.after(0, lambda: self.notebook.select(self.tab_results))
            self.root.after(0, lambda: messagebox.showinfo("Done", f"Organized {len(organized)} files."))
        except Exception as e:
            logger.exception("Error during process")
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.root.after(0, lambda: self.btn_scan.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_stop.config(state=tk.DISABLED))
            self.root.after(0, lambda: self.lbl_status.config(text="Ready"))

    def _cleanup_workspace(self):
        work_dir = Path(self.var_db_path.get())
        if not work_dir.exists(): return

        self._update_status("Cleaning up old reports...")
        for pattern in ["sherlock_ai_context_*.json", "master_media_directory_*.csv"]:
            files = sorted(list(work_dir.glob(pattern)), key=lambda x: x.stat().st_mtime, reverse=True)
            for old_file in files[5:]:
                try:
                    old_file.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete old report {old_file}: {e}")

    def on_exit(self):
        if self.db:
            self.db.close()
        self.root.destroy()

    def _export_ai(self):
        trips = self.db.get_all_trips()
        if not trips: return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(self.var_db_path.get()) / f"sherlock_ai_context_{ts}.json"
        self.intelligence.export_for_ai(trips, str(out))
        messagebox.showinfo("Done", f"Exported to {out}")
        webbrowser.open(str(out.parent))

    def _export_csv(self):
        media = self.db.get_all_media()
        if not media: return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        df = pd.DataFrame([f.to_dict() for f in media])
        out = Path(self.var_db_path.get()) / f"master_media_directory_{ts}.csv"
        df.to_csv(out, index=False)
        messagebox.showinfo("Done", f"Exported to {out}")
        webbrowser.open(str(out.parent))

    def _generate_dashboard(self):
        trips = self.db.get_all_trips()
        media = self.db.get_media_with_gps()
        gen = DashboardGenerator()
        out = Path(self.var_db_path.get())
        dash = gen.generate(trips, media, out)
        webbrowser.open(str(dash))
