import os
import shutil
import threading
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.models import MediaFile

logger = logging.getLogger(__name__)

class MediaOrganizer:
    def __init__(self, dest_path: str, overflow_path: str = None, request_overflow_cb=None):
        self.dest_paths = [Path(dest_path)]
        if overflow_path:
            self.dest_paths.append(Path(overflow_path))
        self.request_overflow_cb = request_overflow_cb
        self.discard_path = Path(dest_path) / "discard"
        self.lock = threading.Lock()
        
        # Ensure base directories exist
        for p in self.dest_paths:
            p.mkdir(parents=True, exist_ok=True)
        self.discard_path.mkdir(parents=True, exist_ok=True)

    def _get_target_base(self, file_size: int) -> Path:
        """Determines which destination path should be used, requesting more if needed."""
        buffer = 1024 * 1024 * 1024 # 1GB buffer
        
        with self.lock:
            for p in self.dest_paths:
                try:
                    usage = shutil.disk_usage(p)
                    if usage.free > file_size + buffer:
                        return p
                except Exception as e:
                    logger.warning(f"Failed to check disk usage for {p}: {e}")
                    continue

            if self.request_overflow_cb:
                logger.info("Main storage full, requesting overflow path...")
                new_path = self.request_overflow_cb()
                if new_path:
                    new_path = Path(new_path)
                    new_path.mkdir(parents=True, exist_ok=True)
                    self.dest_paths.append(new_path)
                    return new_path
            
            return self.dest_paths[0]

    def organize(self, trip_groups: List[Tuple['Trip', List[MediaFile]]], status_cb=None, delete_source=False, org_mode="Location", home_mapping=None) -> List[MediaFile]:
        processed_files = []
        total_trips = len(trip_groups)
        source_paths = []
        
        potential_duplicates_path = self.discard_path / "potential_duplicates"
        potential_duplicates_path.mkdir(parents=True, exist_ok=True)
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            
            for i, (trip, group) in enumerate(trip_groups):
                if status_cb:
                    status_cb(f"Preparing trip {i+1}/{total_trips}: {trip.name}...")

                sub_path_base = Path(trip.folder_path) / trip.name
                
                # 1. Binary Duplicate Detection
                binary_groups: Dict[str, List[MediaFile]] = {}
                for f in group:
                    binary_groups.setdefault(f.hash, []).append(f)

                unique_files = []
                for b_hash, b_group in binary_groups.items():
                    b_group.sort(key=lambda x: (x.resolution, x.size), reverse=True)
                    winner = b_group[0]
                    losers = b_group[1:]
                    
                    unique_files.append(winner)
                    for loser in losers:
                        source_paths.append(loser.path)
                        futures.append(executor.submit(
                            self._move_file_wrapper, loser, self.discard_path, Path("duplicates"), True, delete_source
                        ))

                # 2. Perceptual Duplicate Detection (among binary unique files)
                perceptual_groups: Dict[str, List[MediaFile]] = {}
                for f in unique_files:
                    p_hash = f.perceptual_hash
                    if p_hash:
                        perceptual_groups.setdefault(p_hash, []).append(f)
                    else:
                        # Files without p_hash are treated as unique in this step
                        perceptual_groups.setdefault(f"no_phash_{f.path}", []).append(f) # hack to keep them separate

                final_winners = []
                for p_hash, p_group in perceptual_groups.items():
                    if p_hash.startswith("no_phash_"):
                        final_winners.append(p_group[0])
                        continue
                        
                    p_group.sort(key=lambda x: (x.resolution, x.size), reverse=True)
                    winner = p_group[0]
                    losers = p_group[1:]
                    
                    final_winners.append(winner)
                    for loser in losers:
                        source_paths.append(loser.path)
                        futures.append(executor.submit(
                            self._move_file_wrapper, loser, potential_duplicates_path, Path(trip.name), True, delete_source
                        ))

                # Identify the primary location name (before trip name/date suffix)
                # We use the trip.folder_path which initially contains the sanitized top_loc
                primary_loc_name = trip.folder_path
                
                # Check if trip has multiple unique locations and determine if subfolders are needed
                unique_locs = {f.location for f in final_winners if f.location and f.location != "Unknown"}
                has_multiple_locations = len(unique_locs) > 1

                trip_target_dir_set = False

                for winner in final_winners:
                    if org_mode == "Location":
                        raw_loc = winner.location or "Unknown Location"
                        if home_mapping and raw_loc in home_mapping:
                            raw_loc = home_mapping[raw_loc]
                            
                        sanitized_loc = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in raw_loc).strip()
                        sanitized_loc = sanitized_loc.replace(", ", "_").replace(" ", "_")
                        sub_path = Path(sanitized_loc)
                        
                        # Set trip folder path for Dashboard compatibility
                        sub_path_base = sub_path
                    else:
                        # EXISTING LOGIC FOR TRIPS
                        if has_multiple_locations and winner.location and winner.location != "Unknown":
                            # Compare sanitized versions to ensure match even with special characters
                            sanitized_winner_loc = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in winner.location).strip()
                            sanitized_primary_loc = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in primary_loc_name).strip()
                            
                            if sanitized_winner_loc == sanitized_primary_loc:
                                sub_path = sub_path_base
                            else:
                                loc_folder = sanitized_winner_loc.replace(", ", "_").replace(" ", "_")
                                sub_path = sub_path_base / loc_folder
                        else:
                            sub_path = sub_path_base

                    source_paths.append(winner.path)
                    target_base = self._get_target_base(winner.size)
                    
                    if not trip_target_dir_set:
                        # We explicitly DO NOT merge the absolute `target_base` into the DB path 
                        # so that the SQL Database remains perfectly portable to other machines/drives.
                        trip.folder_path = str(sub_path_base)
                        trip_target_dir_set = True

                    futures.append(executor.submit(
                        self._move_file_wrapper, winner, target_base, sub_path, False, delete_source
                    ))

            completed = 0
            total_files = len(futures)
            for future in as_completed(futures):
                media, final_path = future.result()
                if final_path:
                    media.path = str(final_path)
                    # Check if the final path is NOT inside the discard directory
                    if not str(final_path).startswith(str(self.discard_path)):
                         processed_files.append(media)
                
                completed += 1
                if status_cb and completed % 20 == 0:
                    status_cb(f"Moving files: {completed}/{total_files}...")

        if delete_source:
            self._cleanup_empty_folders(source_paths, status_cb)

        logger.info(f"Organization complete. Processed {len(processed_files)} unique files.")
        return processed_files

    def _move_file_wrapper(self, media: MediaFile, base_dir: Path, sub_path: Path, is_discard: bool, delete_source: bool):
        try:
            final_path = self._move_file(media, base_dir, sub_path, is_discard, delete_source)
            return media, final_path
        except Exception as e:
            logger.error(f"Error moving {media.path}: {e}")
            return media, None

    def _move_file(self, media: MediaFile, base_dir: Path, sub_path: Path, is_discard: bool = False, delete_source: bool = False) -> Path:
        target_dir = base_dir / sub_path
        
        with self.lock:
            target_dir.mkdir(parents=True, exist_ok=True)
        
        filename = Path(media.path).name
        target_path = target_dir / filename
        
        counter = 1
        while target_path.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            target_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        try:
            if delete_source:
                shutil.move(media.path, target_path)
            else:
                shutil.copy2(media.path, target_path)
            return target_path
        except Exception as e:
            logger.error(f"Failed to move/copy {media.path} to {target_path}: {e}")
            raise e

    def _cleanup_empty_folders(self, file_paths: List[str], status_cb=None):
        if status_cb:
            status_cb("Cleaning up empty source folders...")
        
        # Collect all immediate parents and their ancestors to safely bubble-up deletions
        folders = set()
        for p in file_paths:
            path_obj = Path(p)
            for parent in path_obj.parents:
                folders.add(str(parent))
                
        # Sort by length descending ensures deepest folders are processed first
        for folder in sorted(list(folders), key=len, reverse=True):
            try:
                if os.path.exists(folder) and not os.listdir(folder):
                    os.rmdir(folder)
                    logger.debug(f"Removed empty folder: {folder}")
            except OSError as e:
                logger.debug(f"Could not remove folder {folder}: {e}")
