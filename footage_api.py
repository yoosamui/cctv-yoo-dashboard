#!/usr/bin/env python3
"""
CCTV Footage API Server - With HTTP Range Support for Video Seeking v1.2.0
"""

import json
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.parse

VERSION = "1.2.0"

STORAGE_ROOT = Path("/media/share/cameras/cctv-storage")
HOST = "0.0.0.0"
PORT = 8881
MAX_ITEMS_PER_PAGE = 12
RESCAN_INTERVAL = 60  # seconds; background auto-rescan so new footage appears automatically
LOOKBACK_HOURS = 2    # time queries show this many hours of history before the searched time

def get_all_files():
    if not STORAGE_ROOT.exists():
        print(f"ERROR: {STORAGE_ROOT} does not exist")
        return []
    
    files = []
    for path in STORAGE_ROOT.rglob("*"):
        if path.is_file():
            name = path.name
            suffix = path.suffix.lower()
            if suffix in {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}:
                # Two timestamps matter. The leading clip time groups a video with its
                # detection images and is what we display/filter by. Detection images
                # also carry a trailing capture time
                # (..._DETECTION_..._2026-06-17_15-12-35-706.jpg), used only to order
                # the images within their clip group.
                timestamp = None  # clip time (leading)
                capture = None    # capture time (trailing); == timestamp for plain clips
                matches = re.findall(r'(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})', name)
                if matches:
                    d0, t0 = matches[0]
                    try:
                        timestamp = datetime.strptime(f"{d0} {t0.replace('-', ':')}", "%Y-%m-%d %H:%M:%S")
                    except:
                        pass
                    d1, t1 = matches[-1]
                    try:
                        capture = datetime.strptime(f"{d1} {t1.replace('-', ':')}", "%Y-%m-%d %H:%M:%S")
                    except:
                        pass
                if capture is None:
                    capture = timestamp

                camera = "Unknown"
                parts = name.replace('.mp4', '').replace('.jpg', '').split('_')
                if len(parts) >= 3:
                    camera = parts[2]

                kind = "Video" if suffix in {'.mp4', '.mov', '.mkv', '.avi', '.webm'} else "Image"
                files.append({
                    "name": name,
                    "path": str(path),
                    "camera": camera,
                    "kind": kind,
                    "timestamp": timestamp,
                    "capture": capture,
                    "size_bytes": path.stat().st_size,
                })
    
    files.sort(key=lambda x: (x["timestamp"] or datetime.min, x["capture"] or datetime.min), reverse=True)
    return files

print("Loading files from storage...")
ALL_FILES = get_all_files()
print(f"Ready! Found {len(ALL_FILES)} files.")

def auto_rescan_loop():
    """Periodically refresh ALL_FILES so newly recorded footage appears automatically."""
    global ALL_FILES
    while True:
        time.sleep(RESCAN_INTERVAL)
        try:
            files = get_all_files()
            ALL_FILES = files  # atomic rebind; readers see either old or new list
        except Exception as e:
            print(f"[auto-rescan] error: {e}")

def latest_date_for(camera_name=None):
    """Return the most recent date that has footage (optionally for one camera)."""
    latest = None
    for f in ALL_FILES:
        if not f["timestamp"]:
            continue
        if camera_name and f["camera"].lower() != camera_name.lower():
            continue
        d = f["timestamp"].date()
        if latest is None or d > latest:
            latest = d
    return latest

def latest_date_with_hour(camera_name, hour):
    """Most recent date that has footage within the given hour (HH:00-HH:59).

    Used to resolve the day for time-only queries (e.g. "16:00") so they land
    on the latest day that actually has footage near that hour.
    """
    latest = None
    for f in ALL_FILES:
        ts = f["timestamp"]
        if not ts:
            continue
        if camera_name and f["camera"].lower() != camera_name.lower():
            continue
        if ts.hour == hour:
            d = ts.date()
            if latest is None or d > latest:
                latest = d
    return latest

def lookback_window(camera_name, target_dt):
    """Window for a time query anchored at target_dt.

    Upper bound = the first clip at/after target_dt on the same day (the
    "nearest" entry point, e.g. 15:04 for a 15:00 search). Lower bound =
    target_dt - LOOKBACK_HOURS. Returns (lower, upper) so results show the
    anchor clip plus the preceding couple of hours.
    """
    upper = None
    for f in ALL_FILES:
        ts = f["timestamp"]
        if not ts:
            continue
        if camera_name and f["camera"].lower() != camera_name.lower():
            continue
        if ts >= target_dt and ts.date() == target_dt.date():
            if upper is None or ts < upper:
                upper = ts
    if upper is None:
        upper = target_dt
    lower = target_dt - timedelta(hours=LOOKBACK_HOURS)
    return lower, upper

def parse_query(query: str):
    if not query:
        return None, None, None, None
    
    # URL decode the query
    query = urllib.parse.unquote(query)
    query = query.strip()
    
    available_cameras = set(f["camera"] for f in ALL_FILES)
    camera_map = {cam.lower(): cam for cam in available_cameras}
    
    # ============================================================
    # SIMPLE APPROACH: Convert underscore format to space format
    # ============================================================
    
    # Replace underscores with spaces, but be careful:
    # We want: Garage_2026-06-14_08:00 -> Garage 2026-06-14 08:00
    # But we need to preserve the camera name separately
    
    parts = query.split('_')
    
    # Check if it's Camera_Date_Time format (3 parts)
    if len(parts) == 3:
        camera_name = parts[0]
        date_str = parts[1]
        time_str = parts[2]
        
        # Check if camera is valid
        if camera_name in available_cameras:
            # Check if date is valid
            date_match = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_str)
            if date_match:
                # Check if time is valid
                time_match = re.match(r'^(\d{1,2})[:](\d{2})$', time_str)
                if not time_match:
                    time_match = re.match(r'^(\d{1,2})[-](\d{2})$', time_str)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        target_dt = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S")
                        lower, upper = lookback_window(camera_name, target_dt)
                        return camera_name, lower, upper, None

    # Check if it's Camera_Date format (2 parts)
    if len(parts) == 2:
        camera_name = parts[0]
        date_str = parts[1]
        
        if camera_name in available_cameras:
            date_match = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_str)
            if date_match:
                start_dt = datetime.strptime(date_str, "%Y-%m-%d")
                end_dt = start_dt.replace(hour=23, minute=59, second=59)
                return camera_name, start_dt, end_dt, None
    
    # Check if it's Date_Time format (2 parts, no camera)
    if len(parts) == 2:
        date_str = parts[0]
        time_str = parts[1]
        date_match = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_str)
        if date_match:
            time_match = re.match(r'^(\d{1,2})[:](\d{2})$', time_str)
            if not time_match:
                time_match = re.match(r'^(\d{1,2})[-](\d{2})$', time_str)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    target_dt = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S")
                    lower, upper = lookback_window(None, target_dt)
                    return None, lower, upper, None

    # Check if it's Camera_Time format (2 parts, no date) -> use latest day with that hour
    if len(parts) == 2:
        camera_name = parts[0]
        time_str = parts[1]
        if camera_name in available_cameras:
            time_match = re.match(r'^(\d{1,2})[:](\d{2})$', time_str)
            if not time_match:
                time_match = re.match(r'^(\d{1,2})[-](\d{2})$', time_str)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    target_date = latest_date_with_hour(camera_name, hour)
                    if target_date:
                        target_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
                        lower, upper = lookback_window(camera_name, target_dt)
                        return camera_name, lower, upper, None

    # ============================================================
    # FALLBACK: Space-separated format
    # ============================================================
    query2 = query.replace('#', ' ')
    original = ' '.join(query2.strip().split())
    words = original.split()
    
    camera_name = None
    remaining = original
    
    if words and words[0].lower() in camera_map:
        camera_name = camera_map[words[0].lower()]
        remaining = ' '.join(words[1:]) if len(words) > 1 else ""
    else:
        remaining = original
    
    if not remaining:
        return camera_name, None, None, None
    
    # Check for "today"
    if remaining.strip().lower() == 'today':
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
        return camera_name, today_start, today_end, None
    
    # Check for "YYYY-MM-DD HH:MM"
    dt_match = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{1,2})[:](\d{2})', remaining)
    if not dt_match:
        dt_match = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{1,2})[-](\d{2})', remaining)
    if dt_match:
        date_str = dt_match.group(1)
        hour = int(dt_match.group(2))
        minute = int(dt_match.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target_dt = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S")
            lower, upper = lookback_window(camera_name, target_dt)
            return camera_name, lower, upper, None
    
    # Check for "YYYY-MM-DD"
    date_match = re.match(r'^(\d{4}-\d{2}-\d{2})$', remaining)
    if date_match:
        start_dt = datetime.strptime(remaining, "%Y-%m-%d")
        end_dt = start_dt.replace(hour=23, minute=59, second=59)
        return camera_name, start_dt, end_dt, None
    
    # Check for "HH:MM" (no date) -> use latest available date
    time_match = re.match(r'^(\d{1,2})[:](\d{2})$', remaining)
    if not time_match:
        time_match = re.match(r'^(\d{1,2})[-](\d{2})$', remaining)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target_date = latest_date_with_hour(camera_name, hour)
            if target_date:
                target_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
                lower, upper = lookback_window(camera_name, target_dt)
                return camera_name, lower, upper, None

    return camera_name, None, None, remaining

def filter_files(files, camera_name, filter1, filter2, text_search, kind_filter=None, order="desc"):
    result = files
    ts_key = lambda x: (x["timestamp"] or datetime.min, x["capture"] or datetime.min)

    if camera_name:
        result = [f for f in result if f["camera"].lower() == camera_name.lower()]

    if kind_filter:
        result = [f for f in result if f["kind"].lower() == kind_filter.lower()]

    if filter1 and filter2 and isinstance(filter1, datetime):
        # filter1..filter2 is the time window built by parse_query (the matching hour
        # for time queries, or the whole day for date/today queries). `order` controls
        # direction: desc (default) = newest-first, asc = oldest-first.
        result = [f for f in result if f["timestamp"] and filter1 <= f["timestamp"] <= filter2]
        result.sort(key=ts_key, reverse=(order != "asc"))
        return result

    if text_search:
        needle = text_search.lower()
        result = [f for f in result if needle in f["name"].lower()]

    result.sort(key=ts_key, reverse=(order != "asc"))
    return result

def serve_html_file(self, filename, content_type="text/html"):
    try:
        with open(filename, 'rb') as f:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            # Don't let browsers cache the UI, so frontend updates take effect on reload
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(f.read())
        return True
    except FileNotFoundError:
        return False

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        
        if path == '/':
            if serve_html_file(self, 'index.html'):
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write("""
<!DOCTYPE html>
<html>
<head>
    <title>CCTV Footage Server</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        h1 { color: #333; }
        .links { margin-top: 30px; }
        .links a { display: inline-block; margin: 10px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; }
        .links a:hover { background: #0056b3; }
    </style>
</head>
<body>
    <h1>CCTV Footage Server</h1>
    <div class="links">
        <a href="/footage">📹 View Footage</a>
        <a href="/infos">ℹ️ System Info</a>
    </div>
</body>
</html>
""")
            return
        
        if path == '/footage':
            if serve_html_file(self, 'footage.html'):
                return
        
        if path == '/infos':
            if serve_html_file(self, 'infos.html'):
                return
        
        if path == '/details':
            if serve_html_file(self, 'details.html'):
                return
        
        if path.endswith('.html'):
            filename = path.lstrip('/')
            if serve_html_file(self, filename):
                return
            self.send_response(404)
            self.end_headers()
            self.wfile.write(f"{filename} not found".encode())
            return
        
        if path == '/api/version':
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"version": VERSION}, indent=2).encode())
            return

        if path == '/api/rescan':
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 Rescanning storage...")
            global ALL_FILES
            ALL_FILES = get_all_files()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ Rescan complete! Found {len(ALL_FILES)} files.")
            
            response = {
                "status": "success",
                "message": f"Rescan complete. Found {len(ALL_FILES)} files.",
                "total_files": len(ALL_FILES),
                "timestamp": datetime.now().isoformat()
            }
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response, indent=2).encode())
            return
        
        if path == '/api/footage':
            query = params.get("query", [""])[0].strip()
            kind = params.get("kind", [""])[0].strip()
            try:
                page = int(params.get("page", ["1"])[0])
            except ValueError:
                page = 1
            if page < 1:
                page = 1
            order = params.get("order", ["desc"])[0].strip().lower()
            if order not in ("asc", "desc"):
                order = "desc"

            camera_name, filter1, filter2, text_search = parse_query(query)
            filtered = filter_files(ALL_FILES, camera_name, filter1, filter2, text_search, kind if kind else None, order)
            
            start = (page - 1) * MAX_ITEMS_PER_PAGE
            end = start + MAX_ITEMS_PER_PAGE
            paginated = filtered[start:end]
            total_pages = (len(filtered) + MAX_ITEMS_PER_PAGE - 1) // MAX_ITEMS_PER_PAGE if filtered else 0
            
            items = []
            for f in paginated:
                items.append({
                    "name": f["name"],
                    "path": f["path"],
                    "camera": f["camera"],
                    "kind": f["kind"],
                    "modified": f["timestamp"].isoformat() if f["timestamp"] else "",
                    "size": f"{f['size_bytes'] / 1024 / 1024:.1f} MB",
                    "bytes": f["size_bytes"],
                })
            
            cameras = sorted(set(f["camera"] for f in ALL_FILES))
            
            response = {
                "version": VERSION,
                "root": str(STORAGE_ROOT),
                "query": query,
                "kind_filter": kind,
                "order": order,
                "camera_filter": camera_name,
                "available_cameras": cameras,
                "total_files": len(ALL_FILES),
                "total_filtered": len(filtered),
                "page": page,
                "total_pages": total_pages,
                "items_per_page": MAX_ITEMS_PER_PAGE,
                "count": len(items),
                "items": items,
            }
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response, indent=2).encode())
            return
        
        if path == '/api/preview':
            raw_path = params.get("path", [""])[0]
            if raw_path:
                decoded_path = urllib.parse.unquote(raw_path)
                target = Path(decoded_path)
                
                if target.exists() and target.is_file():
                    file_size = target.stat().st_size
                    mime_type = "video/mp4" if target.suffix.lower() == '.mp4' else "image/jpeg"
                    range_header = self.headers.get('Range')
                    
                    try:
                        if range_header:
                            range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
                            if range_match:
                                start = int(range_match.group(1))
                                end = range_match.group(2)
                                end = int(end) if end else file_size - 1
                                end = min(end, file_size - 1)
                                content_length = end - start + 1
                                
                                self.send_response(206)
                                self.send_header("Content-Type", mime_type)
                                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                                self.send_header("Content-Length", str(content_length))
                                self.send_header("Accept-Ranges", "bytes")
                                self.send_header("Access-Control-Allow-Origin", "*")
                                self.end_headers()
                                
                                with open(target, "rb") as f:
                                    f.seek(start)
                                    remaining = content_length
                                    while remaining > 0:
                                        chunk_size = min(8192, remaining)
                                        chunk = f.read(chunk_size)
                                        if not chunk:
                                            break
                                        try:
                                            self.wfile.write(chunk)
                                        except (BrokenPipeError, ConnectionResetError):
                                            return
                                        remaining -= len(chunk)
                                return
                        
                        self.send_response(200)
                        self.send_header("Content-Type", mime_type)
                        self.send_header("Content-Length", str(file_size))
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        
                        with open(target, "rb") as f:
                            chunk_size = 8192
                            while True:
                                chunk = f.read(chunk_size)
                                if not chunk:
                                    break
                                try:
                                    self.wfile.write(chunk)
                                except (BrokenPipeError, ConnectionResetError):
                                    return
                        return
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception as e:
                        print(f"Error serving file {target}: {e}")
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b"Internal Server Error")
                        return
            
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not found")
    
    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    print("=" * 50)
    print("CCTV Footage API Server - With Rescan Support")
    print("=" * 50)
    print(f"Storage: {STORAGE_ROOT}")
    print(f"Open in browser: http://{HOST}:{PORT}")
    print(f"  - http://{HOST}:{PORT}/ -> index.html")
    print(f"  - http://{HOST}:{PORT}/footage -> footage.html")
    print(f"  - http://{HOST}:{PORT}/infos -> infos.html")
    print(f"  - http://{HOST}:{PORT}/details -> details.html")
    print(f"  - http://{HOST}:{PORT}/api/rescan -> Rescan storage folder")
    print(f"Total files: {len(ALL_FILES)}")
    print(f"Items per page: {MAX_ITEMS_PER_PAGE}")
    print(f"Auto-rescan: every {RESCAN_INTERVAL}s")
    print("=" * 50)

    threading.Thread(target=auto_rescan_loop, daemon=True).start()

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        server.serve_forever()
    except OSError as e:
        print(f"ERROR: Cannot bind to {HOST}:{PORT}")
        print(f"Reason: {e}")
