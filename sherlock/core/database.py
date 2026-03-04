import sqlite3
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from .models import MediaFile, Trip

logger = logging.getLogger(__name__)

class SherlockDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path / "sherlock.db"
        self._conn = None
        self._lock = threading.RLock()
        self._init_db()

    @property
    def connection(self):
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                # Performance optimizations
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
                self._conn.execute("PRAGMA cache_size = -2000") # 2MB cache
            return self._conn

    def _init_db(self):
        try:
            with self._lock:
                conn = self.connection
                with conn:
                    # Media Table
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS media (
                            hash TEXT,
                            perceptual_hash TEXT,
                            path TEXT PRIMARY KEY,
                            size INTEGER,
                            mtime REAL,
                            date TEXT,
                            device TEXT,
                            lat REAL,
                            lon REAL,
                            altitude REAL,
                            location TEXT,
                            resolution INTEGER,
                            owner TEXT,
                            trip_id INTEGER,
                            device_nickname TEXT
                        )
                    """)
                    # Index for fast lookup
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_hash ON media(hash)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_phash ON media(perceptual_hash)")
                    # Trip Table
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS trips (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT,
                            start_date TEXT,
                            end_date TEXT,
                            locations TEXT,  -- JSON list
                            folder_path TEXT,
                            file_count INTEGER,
                            primary_device TEXT,
                            participants TEXT -- JSON list
                        )
                    """)
                    # Config Table (key-value store)
                    conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
                    # Activity Log
                    conn.execute("CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, timestamp TEXT, action TEXT, details TEXT)")

                    # Migrations
                    self._run_migrations(conn)
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def _run_migrations(self, conn):
        # Migration: Add altitude to media if missing
        try:
            conn.execute("ALTER TABLE media ADD COLUMN altitude REAL")
        except sqlite3.OperationalError:
            pass 

        # Migration: Add perceptual_hash to media if missing
        try:
            conn.execute("ALTER TABLE media ADD COLUMN perceptual_hash TEXT")
        except sqlite3.OperationalError:
            pass

        # Migration: Add mtime to media if missing
        try:
            conn.execute("ALTER TABLE media ADD COLUMN mtime REAL")
        except sqlite3.OperationalError:
            pass

        # Migration: Add participants to trips if missing
        try:
            conn.execute("ALTER TABLE trips ADD COLUMN participants TEXT")
        except sqlite3.OperationalError:
            pass

    def save_media(self, media: MediaFile):
        self.save_media_batch([media])

    def save_media_batch(self, media_list: list[MediaFile]):
        if not media_list: return
        try:
            with self._lock:
                with self.connection:
                    self.connection.executemany("""
                        INSERT OR REPLACE INTO media 
                        (hash, perceptual_hash, path, size, mtime, date, device, lat, lon, altitude, location, resolution, owner, trip_id, device_nickname)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [
                        (
                            m.hash, m.perceptual_hash, m.path, m.size, m.mtime, m.date.isoformat(), 
                            m.device, m.lat, m.lon, m.altitude, m.location, 
                            m.resolution, m.owner, m.trip_id, m.device_nickname
                        ) for m in media_list
                    ])
        except Exception as e:
            logger.error(f"Failed to save media batch: {e}")

    def get_media_by_hash(self, file_hash: str):
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM media WHERE hash = ?", (file_hash,))
            row = cursor.fetchone()
            return self._row_to_media(row) if row else None

    def get_media_by_path(self, path: str):
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM media WHERE path = ?", (path,))
            row = cursor.fetchone()
            return self._row_to_media(row) if row else None

    def _row_to_media(self, row):
        return MediaFile(
            hash=row['hash'], 
            perceptual_hash=row['perceptual_hash'],
            path=row['path'], 
            size=row['size'], 
            mtime=row['mtime'] or 0.0,
            date=datetime.fromisoformat(row['date']), 
            device=row['device'],
            lat=row['lat'], lon=row['lon'], 
            altitude=row['altitude'],
            location=row['location'], 
            resolution=row['resolution'], owner=row['owner'], 
            trip_id=row['trip_id'],
            device_nickname=row['device_nickname']
        )

    def save_trip(self, trip: Trip):
        try:
            locations_json = json.dumps(trip.locations)
            participants_json = json.dumps(trip.participants)
            with self._lock:
                with self.connection:
                    if trip.id is None:
                        cursor = self.connection.execute("""
                            INSERT INTO trips (name, start_date, end_date, locations, folder_path, file_count, primary_device, participants)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (trip.name, trip.start_date.isoformat(), trip.end_date.isoformat(), 
                              locations_json, trip.folder_path, trip.file_count, trip.primary_device, participants_json))
                        trip.id = cursor.lastrowid
                    else:
                        self.connection.execute("""
                            UPDATE trips SET name=?, start_date=?, end_date=?, locations=?, folder_path=?, file_count=?, primary_device=?, participants=?
                            WHERE id=?
                        """, (trip.name, trip.start_date.isoformat(), trip.end_date.isoformat(), 
                              locations_json, trip.folder_path, trip.file_count, trip.primary_device, participants_json, trip.id))
                return trip.id
        except Exception as e:
            logger.error(f"Failed to save trip: {e}")
            return None

    def get_all_trips(self):
        trips = []
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM trips")
            rows = cursor.fetchall()
        
        for row in rows:
            participants = []
            if row['participants']:
                try:
                    participants = json.loads(row['participants'])
                except: pass
            
            trips.append(Trip(
                id=row['id'], name=row['name'], 
                start_date=datetime.fromisoformat(row['start_date']), 
                end_date=datetime.fromisoformat(row['end_date']),
                locations=json.loads(row['locations']), folder_path=row['folder_path'],
                file_count=row['file_count'], primary_device=row['primary_device'],
                participants=participants
            ))
        return trips

    def get_all_media(self):
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM media")
            rows = cursor.fetchall()
        return [self._row_to_media(row) for row in rows]

    def get_media_with_gps(self):
        with self._lock:
            cursor = self.connection.execute("SELECT * FROM media WHERE lat IS NOT NULL AND lon IS NOT NULL")
            rows = cursor.fetchall()
        return [self._row_to_media(row) for row in rows]

    def get_config(self, key, default=None):
        with self._lock:
            cursor = self.connection.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
        return row[0] if row else default

    def save_config(self, key, value):
        try:
            with self._lock:
                with self.connection:
                    self.connection.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        except Exception as e:
            logger.error(f"Failed to save config {key}: {e}")

    def get_owner_mapping(self) -> dict:
        mapping_str = self.get_config('owner_mapping', '{}')
        try:
            return json.loads(mapping_str)
        except:
            return {}

    def save_owner_mapping(self, mapping: dict):
        self.save_config('owner_mapping', json.dumps(mapping))

    def get_home_mapping(self) -> dict:
        mapping_str = self.get_config('home_mapping', '{}')
        try:
            return json.loads(mapping_str)
        except:
            return {}

    def save_home_mapping(self, mapping: dict):
        self.save_config('home_mapping', json.dumps(mapping))

    def log_activity(self, action, details):
        try:
            with self._lock:
                with self.connection:
                    self.connection.execute("INSERT INTO activity (timestamp, action, details) VALUES (?, ?, ?)",
                                 (datetime.now().isoformat(), action, json.dumps(details)))
        except Exception as e:
            logger.error(f"Failed to log activity: {e}")

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
