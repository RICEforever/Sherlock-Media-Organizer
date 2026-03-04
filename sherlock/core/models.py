from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict

@dataclass
class MediaFile:
    path: str
    hash: str  # Binary content hash (SHA-256)
    perceptual_hash: Optional[str] = None # Visual hash (pHash)
    size: int = 0
    date: datetime = field(default_factory=datetime.now)
    device: str = "Unknown"
    mtime: float = 0.0
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude: Optional[float] = None
    location: str = "Unknown"
    resolution: int = 0
    owner: str = "Unknown"
    trip_id: Optional[int] = None
    device_nickname: str = "Unknown"

    def to_dict(self):
        return {
            'path': self.path,
            'hash': self.hash,
            'perceptual_hash': self.perceptual_hash,
            'size': self.size,
            'mtime': self.mtime,
            'date': self.date.isoformat(),
            'device': self.device,
            'lat': self.lat,
            'lon': self.lon,
            'altitude': self.altitude,
            'location': self.location,
            'resolution': self.resolution,
            'owner': self.owner,
            'trip_id': self.trip_id,
            'device_nickname': self.device_nickname
        }

@dataclass
class Trip:
    id: int
    name: str  # e.g. "Paris, France"
    start_date: datetime
    end_date: datetime
    locations: List[str] = field(default_factory=list)
    folder_path: Optional[str] = None  # Where these files ended up stored
    file_count: int = 0
    primary_device: str = "Unknown"
    participants: List[str] = field(default_factory=list)
    peak_altitude: Optional[float] = None
    total_resolution: int = 0

    def to_ai_context(self):
        """Export a compact JSON for AI Context."""
        return {
            "trip_name": self.name,
            "dates": f"{self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}",
            "locations": ", ".join(self.locations[:5]),  # Top 5 locations to save tokens
            "storage_path": self.folder_path,
            "file_count": self.file_count,
            "primary_device": self.primary_device,
            "participants": ", ".join(self.participants),
            "peak_altitude_meters": round(self.peak_altitude) if self.peak_altitude else None,
            "total_pixels": self.total_resolution
        }
