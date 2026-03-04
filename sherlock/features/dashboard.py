import folium
from folium.plugins import MarkerCluster
from pathlib import Path
from typing import List
import webbrowser
from ..core.models import MediaFile, Trip

CSS_TEMPLATE = """
body { font-family: 'Segoe UI', sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
h1 { color: #bb86fc; text-align: center; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 30px; }
.stat-card { background: #1e1e1e; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); text-align: center; }
.stat-val { font-size: 2em; font-weight: bold; color: #03dac6; }
.stat-label { color: #a0a0a0; margin-top: 5px; }
.map-container { height: 500px; width: 100%; border-radius: 8px; overflow: hidden; margin-top: 15px; }
"""

class DashboardGenerator:
    def generate(self, trips: List[Trip], media_with_gps: List[MediaFile], output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Generate Map
        map_path = output_dir / "map_widget.html"
        self._create_map(media_with_gps, map_path)
        
        # 2. Generate Dashboard HTML
        dash_path = output_dir / "dashboard.html"
        html = self._create_html(trips, len(media_with_gps))
        dash_path.write_text(html, encoding="utf-8")
        
        return dash_path

    def _create_map(self, media_files, path):
        if not media_files:
            path.write_text("No GPS data found.", encoding="utf-8")
            return

        sorted_media = sorted(media_files, key=lambda x: x.date)
        start_loc = [sorted_media[0].lat, sorted_media[0].lon]
        
        m = folium.Map(location=start_loc, zoom_start=4, tiles="cartodb dark_matter")
        cluster = MarkerCluster().add_to(m)
        
        coords = []
        for f in sorted_media:
            coords.append([f.lat, f.lon])
            alt_info = f"<br>Alt: {round(f.altitude, 1)}m" if f.altitude is not None else ""
            nick_info = f"<br>Device: {f.device_nickname}" if f.device_nickname else ""
            popup = f"<b>{f.location}</b><br>{f.date.strftime('%Y-%m-%d %H:%M')}{alt_info}{nick_info}"
            folium.Marker([f.lat, f.lon], popup=popup).add_to(cluster)
            
        if len(coords) > 1:
            folium.PolyLine(coords, color="#03dac6", weight=2, opacity=0.7).add_to(m)
            
        m.save(str(path))

    def _create_html(self, trips, gps_count):
        total_files = sum(t.file_count for t in trips)
        trip_rows = ""
        for t in trips:
            participants = ", ".join(t.participants) if t.participants else "Unknown"
            trip_rows += f"<tr><td>{t.name}</td><td>{t.start_date.strftime('%Y-%m-%d')}</td><td>{t.file_count}</td><td>{t.primary_device}</td><td>{participants}</td></tr>"

        return f"""
        <html><head><style>{CSS_TEMPLATE} table {{ width:100%; text-align:left; border-collapse:collapse; margin-top:10px; }} th, td {{ padding: 12px; border-bottom: 1px solid #333; }} </style></head>
        <body>
            <h1>Sherlock Media Dashboard</h1>
            <div class="stats-grid">
                <div class="stat-card"><div class="stat-val">{len(trips)}</div><div class="stat-label">Trips</div></div>
                <div class="stat-card"><div class="stat-val">{total_files}</div><div class="stat-label">Total Files</div></div>
                <div class="stat-card"><div class="stat-val">{gps_count}</div><div class="stat-label">GPS Tagged</div></div>
            </div>
            
            <div class="map-container"><iframe src="map_widget.html" width="100%" height="100%" frameborder="0"></iframe></div>
            
            <h2 style="color:#cf6679; margin-top:30px;">Trip Log</h2>
            <table>
                <tr style="background:#333; color:#bb86fc;"><th>Trip Name</th><th>Date</th><th>Files</th><th>Storage Device</th><th>Participants</th></tr>
                {trip_rows}
            </table>
        </body></html>
        """
