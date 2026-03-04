
import os
import sys
import logging
import json
from pathlib import Path
from datetime import datetime

# Add the parent directory to sys.path to resolve relative import issues.
parent_dir = os.path.dirname(os.path.abspath(__file__))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from sherlock.core.database import SherlockDB
from sherlock.features.scanner import MediaScanner
from sherlock.features.intelligence import TripIntelligence
from sherlock.features.organizer import MediaOrganizer

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("headless_sherlock.log", encoding='utf-8')
        ]
    )

def run_headless(work_dir, sources, primary_dest, overflow_dest=None, delete_source=False):
    setup_logging()
    logger = logging.getLogger("HeadlessSherlock")
    logger.info("Starting Headless Sherlock Media Organiser...")

    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)
    
    db = SherlockDB(work_path)
    
    # Load config from DB or use defaults
    gap_hours = db.get_config('trip_gap_hours', "72")
    distance_km = db.get_config('trip_distance_km', "100")
    default_owner = db.get_config('default_owner', "Unknown")
    
    hc_raw = db.get_config('home_cities', "[]")
    home_cities = json.loads(hc_raw)

    intelligence = TripIntelligence({
        'trip_gap_hours': gap_hours,
        'trip_distance_km': distance_km,
        'home_cities': home_cities
    })

    def status_cb(msg):
        logger.info(f"Status: {msg}")

    def progress_cb(val, total):
        if total > 0:
            percent = (val / total) * 100
            if val % max(1, (total // 10)) == 0 or val == total:
                logger.info(f"Progress: {val}/{total} ({percent:.1f}%)")

    scanner = MediaScanner(status_cb=status_cb, progress_cb=progress_cb)

    logger.info(f"Scanning sources: {sources}")
    scanned_media = scanner.scan(sources, db=db)
    
    if not scanned_media:
        logger.info("No media found to process.")
        return

    db.save_media_batch(scanned_media)
    
    logger.info("Analyzing trips...")
    trip_groups = intelligence.process_trips(scanned_media)
    
    logger.info(f"Identified {len(trip_groups)} trips.")

    logger.info("Organizing files...")
    organizer = MediaOrganizer(
        dest_path=primary_dest,
        overflow_path=overflow_dest
    )
    
    organized_files = organizer.organize(trip_groups, status_cb=status_cb, delete_source=delete_source)
    
    logger.info("Saving trip data to database...")
    all_final_media = []
    for trip, group_files in trip_groups:
        trip_id = db.save_trip(trip)
        for f in group_files:
            f.trip_id = trip_id
        all_final_media.extend(group_files)
    
    db.save_media_batch(all_final_media)
    
    logger.info(f"Successfully organized {len(organized_files)} files.")
    db.close()

if __name__ == "__main__":
    # You can customize these paths as needed for a test run
    # For now, I'll just check if the script runs with mock/empty paths
    if len(sys.argv) < 4:
        print("Usage: python headless_runner.py <work_dir> <source_dir1,source_dir2> <dest_dir>")
    else:
        work_dir = sys.argv[1]
        sources = sys.argv[2].split(',')
        dest_dir = sys.argv[3]
        run_headless(work_dir, sources, dest_dir)
