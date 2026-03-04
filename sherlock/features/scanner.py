import os
import hashlib
import subprocess
import json
import re
import logging
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Callable, Tuple, Dict

from PIL import Image, ExifTags
import imagehash
from ..core.models import MediaFile

logger = logging.getLogger(__name__)

# Register HEIF support if available
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    logger.debug("pillow_heif not found, HEIF support disabled.")

# Try importing optional accelerators
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    logger.debug("opencv-python not found, video pHash support limited.")

try:
    import xxhash
    HAS_XXHASH = True
except ImportError:
    HAS_XXHASH = False
    logger.debug("xxhash not found, falling back to md5.")

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

class MediaScanner:
    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp', '.heic'}
    VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.cr2', '.arw', '.dng', '.3gp'}
    FAST_HASH_THRESHOLD = 50 * 1024 * 1024 # 50MB
    
    def __init__(self, status_cb: Callable = None, progress_cb: Callable = None):
        self.status_cb = status_cb or (lambda x: None)
        self.progress_cb = progress_cb or (lambda x, y: None)
        self.stop_requested = False

    def scan(self, sources: List[str], db=None) -> List[MediaFile]:
        file_paths = []
        for src in sources:
            for root, _, filenames in os.walk(src):
                for name in filenames:
                    path = os.path.join(root, name)
                    ext = Path(path).suffix.lower()
                    if ext in self.IMAGE_EXTS or ext in self.VIDEO_EXTS:
                        file_paths.append(path)
        
        total = len(file_paths)
        self.status_cb(f"Found {total} files. Computing binary signatures...")
        logger.info(f"Starting binary scan of {total} files.")

        # Phase 1: Binary Hashing (Fast)
        media_results: Dict[str, List[MediaFile]] = {} # hash -> list of MediaFile
        
        max_workers = min(os.cpu_count() or 4, 8)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_path = {pool.submit(self._get_binary_info, p): p for p in file_paths}
            
            completed = 0
            for future in as_completed(future_to_path):
                if self.stop_requested: break
                res = future.result()
                if res:
                    h, m_file = res
                    media_results.setdefault(h, []).append(m_file)
                
                completed += 1
                if completed % 50 == 0:
                    self.progress_cb(completed, total)

        if self.stop_requested: return []

        # Phase 2: Metadata Extraction (Only for unique binary hashes)
        unique_hashes = list(media_results.keys())
        total_unique = len(unique_hashes)
        self.status_cb(f"Extracting metadata for {total_unique} unique files...")
        logger.info(f"Extracting metadata for {total_unique} unique items.")

        final_list = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_hash = {pool.submit(self._enrich_metadata, media_results[h][0]): h for h in unique_hashes}
            
            completed = 0
            for future in as_completed(future_to_hash):
                if self.stop_requested: break
                enriched_media = future.result()
                if enriched_media:
                    h = enriched_media.hash
                    # Apply the same metadata to all binary duplicates
                    for i, m in enumerate(media_results[h]):
                        if i == 0:
                            final_list.append(enriched_media)
                        else:
                            # Copy metadata but keep unique path
                            m.date = enriched_media.date
                            m.device = enriched_media.device
                            m.lat = enriched_media.lat
                            m.lon = enriched_media.lon
                            m.altitude = enriched_media.altitude
                            m.resolution = enriched_media.resolution
                            m.perceptual_hash = enriched_media.perceptual_hash
                            final_list.append(m)
                
                completed += 1
                if completed % 20 == 0:
                    self.progress_cb(completed, total_unique)

        return final_list

    def _get_binary_info(self, filepath: str) -> Optional[Tuple[str, MediaFile]]:
        try:
            path = Path(filepath)
            st = path.stat()
            
            # Binary Hash only
            b_hash = self._calculate_binary_hash(filepath)
            if not b_hash: return None

            media = MediaFile(
                path=filepath,
                hash=b_hash,
                size=st.st_size,
                mtime=st.st_mtime
            )
            return b_hash, media
        except Exception as e:
            logger.error(f"Error getting binary info for {filepath}: {e}")
            return None

    def _enrich_metadata(self, media: MediaFile) -> MediaFile:
        """Exhaustive metadata and pHash extraction."""
        try:
            path = Path(media.path)
            ext = path.suffix.lower()
            is_image = ext in self.IMAGE_EXTS
            is_video = ext in self.VIDEO_EXTS

            # 1. Perceptual Hash
            media.perceptual_hash = self._calculate_perceptual_hash(media.path, is_image, is_video)

            # 2. Exhaustive Metadata
            meta = self._get_metadata(media.path)
            media.date = meta['date']
            media.device = meta['device']
            media.lat = meta['lat']
            media.lon = meta['lon']
            media.altitude = meta['altitude']
            media.resolution = meta['width'] * meta['height']
            
            return media
        except Exception as e:
            logger.error(f"Enrichment failed for {media.path}: {e}")
            return media

    def _calculate_binary_hash(self, filepath: str) -> str:
        file_size = os.path.getsize(filepath)
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                if file_size < self.FAST_HASH_THRESHOLD:
                    chunk = f.read(65536)
                    while chunk:
                        hasher.update(chunk)
                        chunk = f.read(65536)
                else:
                    sample_size = 1024 * 1024
                    hasher.update(f.read(sample_size)) # Head
                    f.seek(file_size // 2 - (sample_size // 2))
                    hasher.update(f.read(sample_size)) # Middle
                    f.seek(max(0, file_size - sample_size))
                    hasher.update(f.read(sample_size)) # Tail
                    hasher.update(str(file_size).encode())
            return hasher.hexdigest()
        except Exception: return ""

    def _calculate_perceptual_hash(self, filepath: str, is_image: bool, is_video: bool) -> str:
        phash_str = ""
        try:
            if is_image:
                with Image.open(filepath) as img:
                    phash_str = str(imagehash.phash(img))
            elif is_video and HAS_CV2:
                hashes = []
                with SuppressStderr():
                    cap = cv2.VideoCapture(filepath)
                    if cap.isOpened():
                        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                        if total_frames > 0:
                            for pos_pct in [0.1, 0.5, 0.9]:
                                cap.set(cv2.CAP_PROP_POS_FRAMES, int(total_frames * pos_pct))
                                ret, frame = cap.read()
                                if ret and frame is not None:
                                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                    pil_img = Image.fromarray(frame_rgb)
                                    hashes.append(str(imagehash.phash(pil_img)))
                        cap.release()
                if hashes: phash_str = "_".join(hashes)
        except Exception: pass
        return phash_str

    def _get_metadata(self, filepath: str):
        """Extracts date/gps from Images (PIL) or Videos (FFmpeg)."""
        ext = Path(filepath).suffix.lower()
        res = {
            'date': datetime.fromtimestamp(os.path.getmtime(filepath)),
            'device': 'Unknown',
            'lat': None, 'lon': None, 'altitude': None,
            'width': 0, 'height': 0
        }

        RAW_EXTS = {'.dng', '.cr2', '.arw', '.nef', '.orf', '.rw2'}

        if ext in self.IMAGE_EXTS or ext in RAW_EXTS:
            self._extract_image_meta(filepath, res)
            # If Pillow failed to find EXIF in the RAW file, fallback to ffprobe
            if ext in RAW_EXTS and res.get('lat') is None:
                self._extract_video_meta(filepath, res)
        elif ext in self.VIDEO_EXTS:
            self._extract_video_meta(filepath, res)
            
        return res

    def _extract_image_meta(self, path, res):
        try:
            with Image.open(path) as img:
                res['width'], res['height'] = img.size
                
                # Try getexif() first (better for modern formats/HEIC)
                exif = None
                if hasattr(img, 'getexif'):
                    exif = img.getexif()
                
                if not exif and hasattr(img, '_getexif'):
                    exif = img._getexif()
                
                if not exif: return

                # Date
                date_str = exif.get(36867) or exif.get(306) # DateTimeOriginal or DateTime
                if date_str:
                    try:
                        res['date'] = datetime.strptime(str(date_str).strip(), '%Y:%m:%d %H:%M:%S')
                    except ValueError: pass
                
                # Device
                model = exif.get(272)
                if model: res['device'] = str(model).strip()

                # GPS (Tag 34853 = 0x8825)
                gps_info = None
                if hasattr(exif, 'get_ifd'):
                    try:
                        gps_info = exif.get_ifd(0x8825)
                    except: pass
                
                if not gps_info:
                    gps_info = exif.get(34853)
                
                # If get_ifd wasn't available or failed, get() might return an integer offset.
                # Ensure we only pass a dictionary to _parse_gps.
                if gps_info and isinstance(gps_info, dict):
                    self._parse_gps(gps_info, res)
        except Exception as e:
            logger.debug(f"Image meta extraction failed for {path}: {e}")

    def _extract_video_meta(self, path, res):
        # 1. Try to parse date from filename first (very fast)
        filename = Path(path).name
        date_match = re.search(r'(\d{4})[-_](\d{2})[-_](\d{2})', filename)
        if date_match:
            try:
                res['date'] = datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            except ValueError: pass

        # 2. Try FFprobe for high-fidelity metadata
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', path
            ]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            out = subprocess.check_output(cmd, startupinfo=startupinfo, stderr=subprocess.STDOUT)
            data = json.loads(out)
            
            fmt = data.get('format', {})
            tags = fmt.get('tags', {})
            
            # Resolution from streams
            streams = data.get('streams', [])
            for s in streams:
                if s.get('codec_type') == 'video':
                    res['width'] = int(s.get('width', 0))
                    res['height'] = int(s.get('height', 0))
                    # Some videos store metadata in the stream tags
                    s_tags = s.get('tags', {})
                    if not tags: tags = s_tags
                    break

            # Date
            c_time = tags.get('creation_time') or tags.get('com.apple.quicktime.creationdate')
            if c_time:
                for fmt_str in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"]:
                    try:
                        res['date'] = datetime.strptime(c_time.split('.')[0], fmt_str)
                        break
                    except ValueError: continue

            # Device
            make = tags.get('make') or tags.get('com.apple.quicktime.make')
            model = tags.get('model') or tags.get('com.apple.quicktime.model')
            
            if make and model:
                res['device'] = f"{str(make).strip()} {str(model).strip()}" if str(make).lower() not in str(model).lower() else str(model).strip()
            elif model: res['device'] = str(model).strip()
            elif make: res['device'] = str(make).strip()

            # GPS (Supports ISO6709 and other common string formats)
            loc_str = tags.get('location') or tags.get('com.apple.quicktime.location.ISO6709') or tags.get('location-eng')
            if loc_str:
                # Regex for +27.1234+078.1234 or +27.1234+078.1234+123.456
                match = re.search(r'([+-][0-9.]+)([+-][0-9.]+)([+-][0-9.]+)?', loc_str)
                if match:
                    res['lat'], res['lon'] = float(match.group(1)), float(match.group(2))
                    if match.group(3): res['altitude'] = float(match.group(3))
                else:
                    # Try alternate format: "27.1234, 78.1234"
                    alt_match = re.search(r'([0-9.-]+)\s*,\s*([0-9.-]+)', loc_str)
                    if alt_match:
                        res['lat'], res['lon'] = float(alt_match.group(1)), float(alt_match.group(2))

        except Exception as e:
            logger.debug(f"Video meta extraction failed for {path}: {e}")

    def _parse_gps(self, gps_info, res):
        def _convert(v):
            if v is None: return 0.0
            # Handle Pillow IFDRational or tuples
            try:
                if hasattr(v, 'numerator') and hasattr(v, 'denominator'):
                    return float(v.numerator) / float(v.denominator) if v.denominator != 0 else 0.0
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    return float(v[0]) / float(v[1]) if v[1] != 0 else 0.0
                if isinstance(v, (list, tuple)) and len(v) > 0 and isinstance(v[0], (list, tuple)):
                    return _convert(v[0])
            except: pass
            return float(v)

        def _get_ref(val, default):
            if val is None: return default
            if isinstance(val, bytes): return val.decode('utf-8', 'ignore').strip()
            return str(val).strip()

        try:
            # Handle both integer tags and string tags
            from PIL.ExifTags import GPSTAGS
            data = {}
            if any(isinstance(k, str) for k in gps_info.keys()):
                rev_gps = {v: k for k, v in GPSTAGS.items()}
                for k, v in gps_info.items():
                    if isinstance(k, str) and k in rev_gps:
                        data[rev_gps[k]] = v
                    else:
                        data[k] = v
            else:
                data = gps_info

            lat_raw = data.get(2)
            lon_raw = data.get(4)
            alt_raw = data.get(6)
            lat_ref = _get_ref(data.get(1), 'N')
            lon_ref = _get_ref(data.get(3), 'E')
            alt_ref = data.get(5, 0)

            if lat_raw and lon_raw:
                if isinstance(lat_raw, (list, tuple)) and len(lat_raw) >= 3:
                    lat = _convert(lat_raw[0]) + _convert(lat_raw[1])/60 + _convert(lat_raw[2])/3600
                    lon = _convert(lon_raw[0]) + _convert(lon_raw[1])/60 + _convert(lon_raw[2])/3600
                else:
                    lat = _convert(lat_raw)
                    lon = _convert(lon_raw)

                if lat_ref.upper() == 'S': lat = -lat
                if lon_ref.upper() == 'W': lon = -lon
                res['lat'], res['lon'] = lat, lon

            if alt_raw:
                alt = _convert(alt_raw)
                if str(alt_ref) == '1' or (isinstance(alt_ref, bytes) and alt_ref == b'\x01'): 
                    alt = -alt
                res['altitude'] = alt
        except Exception as e:
            logger.debug(f"GPS parsing failed: {e}")
