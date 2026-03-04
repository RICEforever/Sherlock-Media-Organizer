from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import logging
import re
from geopy.distance import geodesic
from collections import Counter
import json
import os
from pathlib import Path
from ..core.models import MediaFile, Trip

import reverse_geocoder as rg

logger = logging.getLogger(__name__)

class TripIntelligence:
    def __init__(self, config: Dict):
        self.config = config
        self.gap_hours = float(config.get('trip_gap_hours', 72))
        self.distance_km = float(config.get('trip_distance_km', 100))
        self.home_cities = config.get('home_cities', []) 
        self._loc_cache = {} # Cache for geocoding speed

    def update_config(self, config: Dict):
        self.config.update(config)
        self.gap_hours = float(self.config.get('trip_gap_hours', self.gap_hours))
        self.distance_km = float(self.config.get('trip_distance_km', self.distance_km))
        self.home_cities = self.config.get('home_cities', self.home_cities)

    def get_smart_location(self, lat, lon) -> str:
        """Determines a recognizable location name (City/District, State)."""
        if lat is None or lon is None:
            return "Unknown"
        
        cache_key = (round(lat, 3), round(lon, 3))
        if cache_key in self._loc_cache:
            return self._loc_cache[cache_key]
        
        # 1. Check Home Cities
        for home in self.home_cities:
            try:
                dist = geodesic((lat, lon), (home['lat'], home['lon'])).km
                if dist < 2.0: 
                    self._loc_cache[cache_key] = home['name']
                    return home['name']
            except Exception: pass

        # 2. Optimized Reverse Geocode
        try:
            # Force single-process mode (mode=1) to prevent Windows multiprocessing crashes in background threads
            results = rg.search((lat, lon), mode=1)
            if not results: return "Unknown"
            
            res = results[0]
            city = res.get('name', '').strip()
            admin1 = res.get('admin1', '').strip() # State/Province
            admin2 = res.get('admin2', '').strip() # District/County
            cc = res.get('cc', '').strip() 
            
            parts = []
            if cc == 'IN':
                # For India: Priority to City name, but ensure District/State fallback is strong
                # Clean up "District" suffix
                admin2_clean = re.sub(r'\s+District$', '', admin2, flags=re.IGNORECASE).strip()
                
                if city:
                    # If city name is just a number or very short, and we have a district, use district instead
                    if (not city.replace('.','').replace('-','').isalpha() or len(city) < 3) and admin2_clean:
                        parts.append(admin2_clean)
                    else:
                        parts.append(city)
                        # Add district if different and helpful
                        if admin2_clean and admin2_clean.lower() not in city.lower() and city.lower() not in admin2_clean.lower():
                            if len(admin2_clean) > 2:
                                parts.append(admin2_clean)
                elif admin2_clean:
                    parts.append(admin2_clean)
                
                if not parts and admin1:
                    parts.append(admin1)
            else:
                # Global: "City, State"
                if city: parts.append(city)
                if admin1 and admin1 != city and len(admin1) > 2:
                    parts.append(admin1)
                # Fallback to District if City/State are missing
                if not parts and admin2:
                    parts.append(admin2)
            
            # Final fallback to anything available if parts is still empty
            if not parts:
                for fallback in [city, admin2, admin1, cc]:
                    if fallback and len(fallback) > 1:
                        parts = [fallback]
                        break
        
            if not parts: parts = ["Unknown"]
        
            loc_str = ", ".join(parts[:2])
            self._loc_cache[cache_key] = loc_str
            return loc_str
        except Exception as e:
            logger.debug(f"Reverse geocode failed for ({lat}, {lon}): {e}")
            return "Unknown"

    def geocode_media(self, media_files: List[MediaFile]):
        """Runs geocoding on all media files prior to trip processing."""
        for f in media_files:
            if f.lat is not None and (not f.location or f.location == "Unknown"):
                f.location = self.get_smart_location(f.lat, f.lon)

    def process_trips(self, media_files: List[MediaFile], home_mapping: Dict[str, str] = None) -> List[Tuple[Trip, List[MediaFile]]]:
        if not media_files: return []
        if home_mapping is None: home_mapping = {}

        # 1. Device-Aware Metadata Augmentation & Geocoding
        devices = sorted(list(set(f.device_nickname for f in media_files)))
        for dev in devices:
            dev_media = sorted([f for f in media_files if f.device_nickname == dev], key=lambda x: x.date)
            self._smart_augment_metadata(dev_media)

        # Ensure geocoding is done (fallback in case UI step was skipped)
        self.geocode_media(media_files)

        # 2. Identify Home locations (for monthly grouping instead of trip grouping)
        effective_home_names = list(home_mapping.keys())
        
        if not effective_home_names:
            loc_counts = Counter(f.location for f in media_files if f.location and f.location != "Unknown")
            if loc_counts:
                top_loc_name, count = loc_counts.most_common(1)[0]
                if count > 100 or count > (len(media_files) * 0.3):
                    effective_home_names.append(top_loc_name)
                    # For auto-detected home, use the location string itself as the nickname
                    home_mapping[top_loc_name] = top_loc_name
                    logger.info(f"Auto-detected home location: {top_loc_name}")

        # Calculate global top location for naming trips with NO GPS
        all_locs = [f.location for f in media_files if f.location and f.location != "Unknown"]
        global_top_loc = Counter(all_locs).most_common(1)[0][0] if all_locs else "Unknown Location"

        # PHASE 1 & 2: Device Isolation & Multi-Dimensional Segmentation
        raw_segments = []
        for dev in devices:
            dev_media = sorted([f for f in media_files if f.device_nickname == dev], key=lambda x: x.date)
            if not dev_media: continue

            current_group = [dev_media[0]]
            # The first photo defines our initial state
            is_away = dev_media[0].location not in effective_home_names and dev_media[0].location != "Unknown"
            
            for i in range(1, len(dev_media)):
                curr = dev_media[i]
                
                # 1. State Resolution
                curr_is_away = False
                if curr.location != "Unknown":
                    curr_is_away = curr.location not in effective_home_names
                else:
                    # If location is unknown, inherit the current state (The Vacuum)
                    curr_is_away = is_away

                # 2. State Transition Triggers Split
                state_changed = (is_away != curr_is_away)
                
                # 3. Altitude Jump Triggers Split (e.g. Flight or Mountain)
                alt_split = False
                if curr.altitude is not None and dev_media[i-1].altitude is not None:
                    if abs(curr.altitude - dev_media[i-1].altitude) > 300:
                        alt_split = True
                
                # 4. Same-State Extreme Split (e.g. 6-months later, still at home)
                time_split = False
                time_diff_hrs = (curr.date - dev_media[i-1].date).total_seconds() / 3600
                if not is_away and time_diff_hrs > (self.gap_hours * 2): # Home groups can safely span longer
                    time_split = True
                elif is_away and time_diff_hrs > self.gap_hours: # Away groups split if completely silent > 72h
                    # Unless they are in the exact same location!
                    if curr.location == dev_media[i-1].location and curr.location != "Unknown":
                        time_split = False
                    else:
                        time_split = True

                # 5. Distance Displacement Split (Respect user's configured trip_distance_km config)
                dist_split = False
                if is_away and curr.lat is not None:
                    # Find the physical anchor point of the current trip segment
                    anchor = next((f for f in current_group if f.lat is not None), None)
                    if anchor:
                        try:
                            d = geodesic((curr.lat, curr.lon), (anchor.lat, anchor.lon)).km
                            if d > self.distance_km:
                                dist_split = True
                        except:
                            pass

                if state_changed or alt_split or time_split or dist_split:
                    raw_segments.append(current_group)
                    current_group = [curr]
                    is_away = curr_is_away
                else:
                    current_group.append(curr)
                    
            raw_segments.append(current_group)

        # PHASE 3: Refined Cross-Device Merging
        # We merge segments if they are "connected" spatially and temporally
        current_segments = raw_segments
        current_segments.sort(key=lambda x: min(f.date for f in x))
        
        merged_groups = []
        visited = [False] * len(current_segments)

        def are_connected(idx1, idx2):
            g1, g2 = current_segments[idx1], current_segments[idx2]
            
            s1, e1 = min(f.date for f in g1), max(f.date for f in g1)
            s2, e2 = min(f.date for f in g2), max(f.date for f in g2)
            
            l1 = set(f.location for f in g1 if f.location != "Unknown")
            l2 = set(f.location for f in g2 if f.location != "Unknown")
            
            d1 = set(f.device_nickname for f in g1)
            d2 = set(f.device_nickname for f in g2)
            
            is_same_device = not d1.isdisjoint(d2)
            
            # State overlap
            is_home1 = all(l in effective_home_names for l in l1) if l1 else False
            is_home2 = all(l in effective_home_names for l in l2) if l2 else False
            
            # Temporal Overlap (Do the windows intersect?)
            windows_overlap = max(s1, s2) <= min(e1, e2)
            
            # Temporal Gap
            time_gap_hrs = 0
            if s2 > e1: time_gap_hrs = (s2 - e1).total_seconds() / 3600
            elif s1 > e2: time_gap_hrs = (s1 - e2).total_seconds() / 3600
            
            # Spatial Proximity
            shared_loc = not l1.isdisjoint(l2)
            spatial_prox = False
            gps1 = [(f.lat, f.lon) for f in g1 if f.lat is not None]
            gps2 = [(f.lat, f.lon) for f in g2 if f.lat is not None]
            if gps1 and gps2:
                try:
                    for p1 in [gps1[0], gps1[-1]]:
                        for p2 in [gps2[0], gps2[-1]]:
                            if geodesic(p1, p2).km < 50.0: # Increased spatial proximity merge window for off-grid trips
                                spatial_prox = True; break
                        if spatial_prox: break
                except: pass

            # Merging Logic for State Machine

            # 1. At-Home Grouping: Merge homes purely by month/year
            if is_home1 and is_home2:
                if shared_loc:
                    shared_homes = l1.intersection(l2).intersection(effective_home_names)
                    if shared_homes and s1.year == s2.year and s1.month == s2.month:
                        return True
                return False

            # 2. Away (Trip) Grouping: Same Device fragmented clusters
            # Since distance/altitude breaks groups, merge them if gap is short (< 24h)
            if is_same_device and not is_home1 and not is_home2:
                if time_gap_hrs < 24.0:
                    # ONLY reconnect them if they were fractured purely due to NO-GPS! 
                    # If they were cleanly fractured by a >100km distance physical jump, PRESERVE THE SPLIT!
                    # Additionally, refuse to merge if they overlap in time. Overlapping dataless 
                    # segments are massive WhatsApp timelines, not chronological fractures!
                    if not windows_overlap and (not gps1 or not gps2):
                        return True
                    return False

            # 3. Away (Trip) Grouping: Cross-Device Multi-User Sync
            if not is_same_device and not is_home1 and not is_home2:
                # If both are Away and they share a location or overlap in time and space, it's one trip!
                if windows_overlap and (shared_loc or spatial_prox):
                    return True
                # Or if they are on the identical trip timeline but one takes pictures slightly later
                # We specifically remove `not l1 or not l2` here because if a massive 2-month WhatsApp 
                # (no-GPS) block overlaps with 10 different geographic trips, it will swallow all of them!
                if time_gap_hrs < 48.0 and (shared_loc or spatial_prox):
                    return True

            return False

        for i in range(len(current_segments)):
            if visited[i]: continue
            
            # Start a new merged group
            group_indices = [i]
            visited[i] = True
            
            # BFS to find all connected segments
            queue = [i]
            while queue:
                curr = queue.pop(0)
                for j in range(len(current_segments)):
                    if not visited[j] and are_connected(curr, j):
                        visited[j] = True
                        group_indices.append(j)
                        queue.append(j)
            
            # Flatten group
            full_group = []
            for idx in group_indices:
                full_group.extend(current_segments[idx])
            merged_groups.append(full_group)

        final_trips = {}
        for group in merged_groups:
            trip = self._finalize_trip(group, home_mapping, global_top_loc)
            if trip.name in final_trips:
                existing_trip, existing_group = final_trips[trip.name]
                existing_group.extend(group)
                existing_trip.end_date = max(existing_trip.end_date, trip.end_date)
                existing_trip.start_date = min(existing_trip.start_date, trip.start_date)
                existing_trip.file_count += trip.file_count
                
                combined_locs = existing_trip.locations + trip.locations
                # Preserve rank via dict.fromkeys uniqueness
                existing_trip.locations = list(dict.fromkeys(combined_locs))[:10]
                existing_trip.total_resolution += trip.total_resolution
                if trip.peak_altitude:
                    existing_trip.peak_altitude = max(existing_trip.peak_altitude or 0, trip.peak_altitude)
            else:
                final_trips[trip.name] = (trip, group)

        merged_trips = list(final_trips.values())
        merged_trips.sort(key=lambda x: x[0].start_date)
        logger.info(f"Segmented Analysis: Created {len(merged_trips)} cohesive trips.")
        return merged_trips

    def _finalize_trip(self, files: List[MediaFile], home_mapping: Dict[str, str], global_top_loc: str = "Unknown Location") -> Trip:
        def sanitize(name):
            if not name: return "Unknown"
            return re.sub(r'[<>:"/\\|?*]', '_', name)

        # 1. Collect all valid geocoded locations in this trip
        trip_locs = [f.location for f in files if f.location and f.location != "Unknown"]
        
        # 2. Determine top location name for folder
        if trip_locs:
            top_loc = Counter(trip_locs).most_common(1)[0][0]
        else:
            # Check for ANY GPS coordinates in this trip
            gps_coords = [(f.lat, f.lon) for f in files if f.lat is not None]
            if gps_coords:
                # Try geocoding the most common GPS coordinate again
                most_common_gps = Counter(gps_coords).most_common(1)[0][0]
                top_loc = self.get_smart_location(most_common_gps[0], most_common_gps[1])
                
                if top_loc == "Unknown":
                    top_loc = "GPS Location"
            else:
            # Absolute last resort: global fallback for no GPS and no local geocoding
                top_loc = global_top_loc
    
        home_names = list(home_mapping.keys())

        start = min(f.date for f in files)
        end = max(f.date for f in files)
        
        duration_hrs = (end - start).total_seconds() / 3600
        days = (end - start).days
        duration_str = f"{days}d" if days > 0 else "1d"
        
        # Determine if this constitutes a "Trip" or just "Monthly Grouping"
        is_trip = False
        if top_loc not in home_names:
            is_trip = True
        else:
            # Check if the group moved away significantly despite having the same high-level top_loc
            # E.g. 12hr duration and large distance between min/max coordinates
            # We don't have explicit home lat/lon anymore, so we check distance among points in trip
            try:
                lats = [f.lat for f in files if f.lat is not None]
                lons = [f.lon for f in files if f.lon is not None]
                if lats and len(lats) > 1:
                    max_d = geodesic((min(lats), min(lons)), (max(lats), max(lons))).km
                    if max_d > 10.0 and duration_hrs > 12:
                        is_trip = True
                        away_locs = [l for l in trip_locs if l not in home_names]
                        if away_locs:
                            top_loc = Counter(away_locs).most_common(1)[0][0]
            except: pass

        owners = [f.owner for f in files if f.owner and f.owner != "Unknown"]
        participants = sorted(list(set(owners)))
        
        participants_suffix = "_" + "_".join(participants[:3]) if participants else ""
        
        alt_suffix = ""
        alts = [f.altitude for f in files if f.altitude is not None]
        if alts:
            max_alt = max(alts)
            if max_alt > 1000:
                alt_suffix = f"_{round(max_alt)}m"

        # Use the home nickname for the folder if it's a home location
        if not is_trip and top_loc in home_mapping:
            safe_loc = sanitize(home_mapping[top_loc])
        else:
            safe_loc = sanitize(top_loc)

        # Stationary/Home locations are grouped by month, Away trips by full date
        if not is_trip:
            name = f"{safe_loc}_{start.strftime('%Y%m')}{participants_suffix}{alt_suffix}" 
        else:
            name = f"{safe_loc}_{start.strftime('%Y%m%d')}_{duration_str}{participants_suffix}{alt_suffix}"
        
        devs = [f.device_nickname for f in files if f.device_nickname]
        primary_dev = Counter(devs).most_common(1)[0][0] if devs else "Unknown"

        total_resolution = sum(f.resolution for f in files if f.resolution)
        final_peak_alt = max_alt if (alts and max_alt > 0) else None

        return Trip(
            id=None,
            name=name,
            start_date=start,
            end_date=end,
            locations=list(set(trip_locs))[:10],
            folder_path=safe_loc,
            file_count=len(files),
            primary_device=primary_dev,
            participants=participants,
            peak_altitude=final_peak_alt,
            total_resolution=total_resolution
        )

    def _smart_augment_metadata(self, media_files: List[MediaFile]):
        """Attempts to assign locations to no-GPS photos using time-sequence analysis.
        This version is called per-device to ensure high-confidence interpolation."""
        resolved_count = 0
        max_gap_hours = 3.0
        
        for i in range(len(media_files)):
            if media_files[i].lat is not None:
                continue
            
            before = next((media_files[j] for j in range(i-1, -1, -1) if media_files[j].lat is not None), None)
            after = next((media_files[j] for j in range(i+1, len(media_files)) if media_files[j].lat is not None), None)
            
            # 1. Higher confidence interpolation
            if before and after:
                time_gap = (after.date - before.date).total_seconds() / 3600
                if time_gap < 6.0: # Both neighbors within 6 hours
                    try:
                        dist = geodesic((before.lat, before.lon), (after.lat, after.lon)).km
                        # Only interpolate if the camera moved less than 5km (conservative)
                        if dist < 5.0:
                            media_files[i].lat, media_files[i].lon = before.lat, before.lon
                            resolved_count += 1
                            continue
                    except Exception: pass
            
            # 2. Nearest neighbor fallback (very conservative)
            best_neighbor = None
            min_diff = max_gap_hours
            if before:
                diff = (media_files[i].date - before.date).total_seconds() / 3600
                if diff < min_diff:
                    best_neighbor, min_diff = before, diff
            if after:
                diff = (after.date - media_files[i].date).total_seconds() / 3600
                if diff < min_diff:
                    best_neighbor = after
            
            if best_neighbor:
                media_files[i].lat, media_files[i].lon = best_neighbor.lat, best_neighbor.lon
                resolved_count += 1
        
        if resolved_count > 0:
            logger.info(f"Smart Augmentation: Resolved locations for {resolved_count} files.")

    def export_for_ai(self, all_trips: List[Trip], output_path: str):
        try:
            export_data = {
                "metadata": {
                    "generated_at": datetime.now().isoformat(),
                    "total_trips": len(all_trips),
                    "instructions": "This file contains a summary of photo trips. Use it to answer questions about where media is stored."
                },
                "trips": [t.to_ai_context() for t in all_trips]
            }
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2)
            return output_path
        except Exception as e:
            logger.error(f"Failed to export AI context: {e}")
            return None
