#!/usr/bin/env python3

import argparse
import concurrent.futures
import hashlib
import html.parser
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urljoin, urlparse

APP = "PlutoFetch"
HOME = Path.home()
WORKDIR = HOME / APP
SEGDIR = WORKDIR / "segments"

GRAPHQL_URL = "https://pluto.tv/api/tn/app-shell/graphql/"
PLAYOUT_BASE = "https://ipv4.pluto.tv/api/tn/video/playout/"

UA = (
    "Mozilla/5.0 (Linux; Android 17; SM-S921B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)

STOP = threading.Event()
RUNNING = set()
RUNNING_LOCK = threading.Lock()


# ---------------- CLI ----------------

def prompt(message):
    print(message, end="", flush=True)
    while not STOP.is_set():
        ready, _, _ = __import__("select").select([sys.stdin], [], [], 0.1)
        if ready:
            value = sys.stdin.readline()
            if not value:
                raise EOFError("Input closed")
            return value.rstrip("\r\n")
    raise KeyboardInterrupt

def parse_args():
    p = argparse.ArgumentParser(
        prog=APP,
        description="Pluto TV downloader using curl and FFmpeg."
    )

    p.add_argument("-U", "--url", required=True,
                   help="Pluto TV episode URL")

    cleanup = p.add_mutually_exclusive_group()
    cleanup.add_argument("-k", action="store_true",
                         help="Keep all temporary files")
    cleanup.add_argument("-ds", action="store_true",
                         help="Delete segments but keep workspace")

    p.add_argument("-n", "--name",
                   help="Custom output filename")

    p.add_argument("-w", "--workers", type=int, default=6,
                   help="Concurrent downloads (default: 6)")

    p.add_argument("-o", "--output", help="Final output directory")
    p.add_argument("--include-filler", action="store_true", help="Include Pluto TV trailing bumpers")
    args = p.parse_args()

    if not 1 <= args.workers <= 32:
        p.error("Workers must be between 1 and 32")

    if urlparse(args.url).hostname not in (
        "pluto.tv", "www.pluto.tv"
    ):
        p.error("Only pluto.tv episode URLs are supported")

    return args


def get_cleanup_mode(args):
    if args.k:
        return "keep"
    if args.ds:
        return "segments"
    return "all"


def cleanup(mode):
    if mode == "keep":
        print(f"Workspace preserved: {WORKDIR}")
    elif mode == "segments":
        shutil.rmtree(SEGDIR, ignore_errors=True)
        print("Segments directory deleted.")
    else:
        shutil.rmtree(WORKDIR, ignore_errors=True)


def check_dependencies():
    for name in ("curl", "ffmpeg"):
        if shutil.which(name) is None:
            raise RuntimeError(
                f"Missing dependency: {name}"
            )


# ---------------- Process control ----------------

def stop_processes():
    STOP.set()

    with RUNNING_LOCK:
        processes = list(RUNNING)

    for proc in processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


def handle_sigint(signum, frame):
    STOP.set()
    raise KeyboardInterrupt


signal.signal(signal.SIGINT, handle_sigint)


def run(command, data=None, capture=True):
    if STOP.is_set():
        raise KeyboardInterrupt

    proc = subprocess.Popen(
        command,
        stdin=(subprocess.PIPE if data is not None
               else subprocess.DEVNULL),
        stdout=(subprocess.PIPE if capture
                else subprocess.DEVNULL),
        stderr=subprocess.PIPE,
    )

    with RUNNING_LOCK:
        RUNNING.add(proc)

    try:
        out, err = proc.communicate(input=data)
    finally:
        with RUNNING_LOCK:
            RUNNING.discard(proc)

    if STOP.is_set():
        raise KeyboardInterrupt

    if proc.returncode:
        message = err.decode(errors="replace").strip()
        raise RuntimeError(
            f"{command[0]} failed: {message}"
        )

    return out or b""


# ---------------- RAM-only cookies ----------------

class MemoryCookies:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def update(self, header_lines, host):
        with self.lock:
            for line in header_lines:
                if not line.lower().startswith("set-cookie:"):
                    continue

                raw = line.split(":", 1)[1].strip()

                try:
                    parsed = SimpleCookie()
                    parsed.load(raw)
                except Exception:
                    continue

                for name, morsel in parsed.items():
                    domain = (
                        morsel["domain"].lstrip(".").lower()
                        or host.lower()
                    )
                    path = morsel["path"] or "/"

                    # Do not accept unrelated cookie domains.
                    if domain != "pluto.tv" and not domain.endswith(
                        ".pluto.tv"
                    ):
                        continue

                    key = (domain, path, name)

                    if morsel["max-age"] == "0" or not morsel.value:
                        self.items.pop(key, None)
                    else:
                        self.items[key] = morsel.value

    def header(self, url):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"

        values = []

        with self.lock:
            for (domain, cookie_path, name), value in self.items.items():
                domain_matches = (
                    host == domain
                    or host.endswith("." + domain)
                )

                if domain_matches and path.startswith(cookie_path):
                    values.append(f"{name}={value}")

        return "; ".join(values)

    def get(self, name):
        with self.lock:
            for (domain, path, key), value in self.items.items():
                if key == name:
                    return value
        return None

    def put(self, name, value):
        with self.lock:
            self.items[("pluto.tv", "/", name)] = value


COOKIES = MemoryCookies()


# ---------------- curl ----------------

def parse_http_response(raw):
    """
    curl -D - writes HTTP headers before the body.
    Multiple header blocks may occur after redirects.
    """

    headers = []
    remaining = raw

    while (
        remaining.startswith(b"HTTP/")
        and (b"\r\n\r\n" in remaining
             or b"\n\n" in remaining)
    ):
        if b"\r\n\r\n" in remaining:
            head, remaining = remaining.split(b"\r\n\r\n", 1)
        else:
            head, remaining = remaining.split(b"\n\n", 1)

        lines = head.decode(
            "latin-1", errors="replace"
        ).splitlines()

        headers.extend(lines)

        # The next bytes may be another header block
        # or the actual response body.
        if not remaining.startswith(b"HTTP/"):
            break

    return headers, remaining


def curl(url, *, post=None, jwt=None, destination=None):
    command = [
        "curl", "-q",
        "--silent", "--show-error", "--fail",
        "--location",
        "--retry", "3",
        "--connect-timeout", "20",
        "-A", UA,
    ]

    host = (urlparse(url).hostname or "").lower()

    # Cookies are never saved to disk.
    if host == "pluto.tv" or host.endswith(".pluto.tv"):
        cookie_header = COOKIES.header(url)

        if cookie_header:
            command += [
                "-H", "Cookie: " + cookie_header
            ]

    if jwt and host == "ipv4.pluto.tv":
        command += [
            "-H", "Authorization: Bearer " + jwt
        ]

    if post is not None:
        command += [
            "-H", "Content-Type: application/json",
            "-H", "Origin: https://pluto.tv",
            "--data-binary", "@-",
        ]

    if destination is None:
        command += ["-D", "-"]
    else:
        partial = Path(str(destination) + ".part")
        command += ["-o", str(partial)]

    command.append(url)

    if destination is None:
        raw = run(command, post)
        headers, body = parse_http_response(raw)

        COOKIES.update(headers, host)
        return body

    partial = Path(str(destination) + ".part")

    try:
        run(command, post)
        partial.replace(destination)
        return destination.stat().st_size
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


# ---------------- Metadata ----------------

class HTMLParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_next = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return

        attrs = dict(attrs)
        self.in_next = attrs.get("id") == "__NEXT_DATA__"

    def handle_data(self, data):
        if self.in_next:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_next = False


def metadata(url):
    page = curl(url).decode("utf-8", errors="replace")

    parser = HTMLParser()
    parser.feed(page)

    if not parser.parts:
        raise RuntimeError(
            "Episode metadata was not found in the page."
        )

    document = json.loads("".join(parser.parts))
    props = document.get("props", {}).get("pageProps", {})
    info = props.get("episodeMetadata") or {}

    episode_match = re.search(
        r"/episode/([a-fA-F0-9]{24})", url
    )

    if not episode_match:
        raise RuntimeError("Invalid episode URL.")

    return {
        "id": episode_match.group(1),
        "series": info.get("seriesTitle") or "Unknown Series",
        "season": info.get("seasonNum") or 0,
        "episode": info.get("episodeNum") or 0,
        "title": info.get("title") or "Unknown Episode",
    }


def filename(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:180] or "video"


def choose_output(args, episode):
    if args.name:
        name = args.name
    else:
        selection = "1"

        if selection == "2":
            name = prompt("Filename: ").strip()
            if not name:
                raise RuntimeError("Empty filename.")
        else:
            name = (
                f"{episode['series']} - "
                f"S{int(episode['season']):02d}"
                f"E{int(episode['episode']):02d} - "
                f"{episode['title']}"
            )

    if name.lower().endswith(".mp4"):
        name = name[:-4]

    return HOME / (filename(name) + ".mp4")


# ---------------- Pluto authentication ----------------

def graphql(query, variables):
    body = json.dumps({
        "query": query,
        "variables": variables
    }).encode()

    response = json.loads(curl(GRAPHQL_URL, post=body))

    if response.get("errors"):
        raise RuntimeError(
            "GraphQL error: " +
            json.dumps(response["errors"], ensure_ascii=False)
        )

    return response.get("data") or {}


def start_session():
    device_id = COOKIES.get("ptv_device_id")

    if not device_id:
        device_id = str(uuid.uuid4())
        COOKIES.put("ptv_device_id", device_id)

    query = """
    query PtvStart($params: StartParameters!) {
      ptvStart(params: $params) {
        deviceId
        session {
          id
          jwt
        }
        refreshInSec
      }
    }
    """

    params = {
        "deviceId": device_id,
        "deviceModel": "Chrome",
        "ptvAppName": "web",
        "cmAudienceID": ""
    }

    result = graphql(query, {"params": params})
    start = result.get("ptvStart") or {}
    session = start.get("session") or {}

    jwt = session.get("jwt")

    if not jwt:
        raise RuntimeError(
            "Pluto did not return a session JWT. "
            "Automatic initialization may not be supported "
            "for this request."
        )

    return jwt


def streaming_url(episode_id, jwt):
    query = """
    query StreamingUrl($params: StreamingUrlParams!) {
      streamingUrl(params: $params) {
        success
        streamingUrl
        url
        stitcherPaths {
          type
          path
        }
      }
    }
    """

    # Read the version returned by Pluto in its JWT,
    # instead of hardcoding an application version.
    import base64

    parts = jwt.split(".")
    if len(parts) != 3:
        raise RuntimeError("Invalid session JWT.")

    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(encoded))

    version = claims.get("appVersion")

    if not version:
        raise RuntimeError(
            "No application version in the session."
        )

    params = {
        "appVersion": version,
        "contentId": episode_id,
        "contentType": "vod",
        "drm": "",
        "drmLevel": "",
        "streamType": "stitcher"
    }

    result = graphql(query, {"params": params})
    stream = result.get("streamingUrl") or {}

    if not stream.get("success"):
        raise RuntimeError(
            "Pluto denied the streaming URL."
        )

    for item in stream.get("stitcherPaths") or []:
        if item.get("type") == "hls":
            return urljoin(
                PLAYOUT_BASE,
                item["path"].lstrip("/")
            )

    direct = stream.get("streamingUrl") or stream.get("url")

    if direct:
        return direct

    raise RuntimeError("No HLS playlist was returned.")


# ---------------- HLS ----------------

def load_playlist(url, jwt):
    text = curl(url, jwt=jwt).decode(
        "utf-8-sig", errors="replace"
    )

    if not text.lstrip().startswith("#EXTM3U"):
        raise RuntimeError("Invalid HLS response.")

    return text


def parse_attributes(line):
    result = {}

    for key, value in re.findall(
        r'([A-Z0-9-]+)=("[^"]*"|[^,]*)',
        line.split(":", 1)[-1]
    ):
        result[key] = value.strip('"')

    return result


def select_variant(master, master_url):
    lines = master.splitlines()
    tracks = {}
    variants = []

    for line in lines:
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attributes(line)

            if attrs.get("TYPE") == "AUDIO":
                tracks.setdefault(
                    attrs.get("GROUP-ID"), []
                ).append(attrs)

    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        attrs = parse_attributes(line)

        uri = next(
            (
                x.strip()
                for x in lines[index + 1:]
                if x.strip() and not x.startswith("#")
            ),
            None
        )

        if not uri:
            continue

        match = re.fullmatch(
            r"(\d+)x(\d+)",
            attrs.get("RESOLUTION", "")
        )

        width, height = (
            map(int, match.groups())
            if match else (0, 0)
        )

        variants.append({
            "url": urljoin(master_url, uri),
            "width": width,
            "height": height,
            "bandwidth": int(attrs.get("BANDWIDTH", 0)),
            "audio": attrs.get("AUDIO")
        })

    if not variants:
        return master_url, None, "Original"

    # Highest resolution if available; otherwise
    # highest advertised bitrate.
    variants.sort(
        key=lambda x: (
            x["width"] * x["height"],
            x["bandwidth"]
        ),
        reverse=True
    )

    selected = variants[0]

    if selected["width"]:
        quality = (
            f"{selected['width']}x{selected['height']}"
        )
    else:
        quality = (
            f"Highest bitrate "
            f"({selected['bandwidth']} bps)"
        )

    audio_url = None
    audio_tracks = tracks.get(selected["audio"], [])

    if audio_tracks:
        audio_tracks.sort(
            key=lambda x: (
                x.get("LANGUAGE", "").lower().startswith("pt"),
                x.get("DEFAULT") == "YES"
            ),
            reverse=True
        )

        uri = audio_tracks[0].get("URI")

        if uri:
            audio_url = urljoin(master_url, uri)

    return selected["url"], audio_url, quality


# ---------------- Segment downloader ----------------

class DownloadQueue:
    def __init__(self):
        self.items = {}

    def add(self, url):
        digest = hashlib.sha256(url.encode()).hexdigest()

        suffix = Path(urlparse(url).path).suffix
        if not suffix or len(suffix) > 8:
            suffix = ".bin"

        name = digest + suffix
        self.items[url] = SEGDIR / name

        return name

    def download(self, jwt, workers):
        jobs = list(self.items.items())
        total = len(jobs)

        if total == 0:
            raise RuntimeError("No media resources were found.")

        completed = 0
        downloaded = 0
        start = time.monotonic()
        last_print = 0.0

        print(f"\nDownloading {total} resources...")

        def worker(job):
            url, path = job

            if STOP.is_set():
                raise KeyboardInterrupt

            if path.exists() and path.stat().st_size > 0:
                return path.stat().st_size

            return curl(
                url,
                jwt=jwt,
                destination=path
            )

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers
        )

        futures = []

        try:
            futures = [
                executor.submit(worker, job)
                for job in jobs
            ]

            future_urls = dict(zip(futures, (url for url, _ in jobs)))

            for future in concurrent.futures.as_completed(futures):
                if STOP.is_set():
                    raise KeyboardInterrupt

                size = future.result()

                completed += 1
                downloaded += size

                now = time.monotonic()

                if now - last_print < 0.5 and completed != total:
                    continue

                last_print = now

                elapsed = max(now - start, 0.001)
                speed = downloaded / elapsed

                avg_size = downloaded / completed
                remaining_bytes = (total - completed) * avg_size

                eta = (
                    remaining_bytes / speed
                    if speed > 0 else None
                )

                percent = completed * 100 / total

                line = (
                    f"[{percent:6.2f}%] "
                    f"{Path(urlparse(future_urls[future]).path).name[:35]:<35} | "
                    f"{completed}/{total} | "
                    f"{format_bytes(downloaded)} | "
                    f"{format_bytes(speed)}/s | "
                    f"ETA {format_eta(eta)}"
                )

                print("\r" + line.ljust(95),
                      end="", flush=True)

        except BaseException:
            stop_processes()

            for future in futures:
                future.cancel()

            raise

        finally:
            executor.shutdown(
                wait=True,
                cancel_futures=True
            )

        print("\nAll resources downloaded.")


def format_bytes(number):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(number)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024


def format_eta(seconds):
    if seconds is None:
        return "Calculating"

    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"


def rewrite_playlist(text, base_url, queue, name):
    result = []
    uri_pattern = re.compile(r'URI="([^"]+)"')

    for line in text.splitlines():
        if "#EXT-X-BYTERANGE" in line:
            raise RuntimeError(
                "Byte-range HLS playlists are not supported."
            )

        if line.startswith("#EXT-X-KEY:"):
            attrs = parse_attributes(line)

            if attrs.get("METHOD") not in ("AES-128", "NONE"):
                raise RuntimeError(
                    "Unsupported HLS encryption method."
                )

            if attrs.get("KEYFORMAT", "identity") != "identity":
                raise RuntimeError(
                    "Unsupported HLS key format."
                )

        if line.startswith("#"):
            def replace(match):
                url = urljoin(base_url, match.group(1))
                local = queue.add(url)
                return f'URI="{local}"'

            result.append(uri_pattern.sub(replace, line))

        elif line.strip():
            url = urljoin(base_url, line.strip())
            result.append(queue.add(url))

        else:
            result.append(line)

    path = SEGDIR / f"{name}.m3u8"
    path.write_text(
        "\n".join(result) + "\n",
        encoding="utf-8"
    )

    return path



def remove_trailing_filler(text, label):
    from urllib.parse import unquote

    keywords = (
        "ident",
        "filler",
        "bumper",
        "outro",
        "endcard",
    )

    lines = text.splitlines()

    boundaries = [
        i for i, line in enumerate(lines)
        if line.strip() == "#EXT-X-DISCONTINUITY"
    ]

    if not boundaries:
        return text

    start = boundaries[-1]
    tail = lines[start + 1:]

    urls = [
        line.strip()
        for line in tail
        if line.strip() and not line.startswith("#")
    ]

    if not urls:
        return text

    duration = 0.0

    for line in tail:
        if line.startswith("#EXTINF:"):
            duration += float(
                line.split(":", 1)[1].split(",", 1)[0]
            )

    if duration <= 0:
        return text

    pattern = re.compile(
        r"(?:^|[^a-z0-9])"
        r"(?:ident|filler|bumper|outro|endcard)"
        r"(?:[^a-z0-9]|$)",
        re.IGNORECASE
    )

    def is_filler(url):
        decoded = url

        for _ in range(3):
            newer = unquote(decoded)
            if newer == decoded:
                break
            decoded = newer

        return bool(pattern.search(decoded))

    if not all(is_filler(url) for url in urls):
        return text

    print(
        f"Skipping trailing filler ({label}): "
        f"{duration:.2f}s, {len(urls)} segments"
    )

    # Keep the playlist ending, but remove the
    # discontinuity and all resources of the filler.
    result = lines[:start]

    if any(
        line.strip() == "#EXT-X-ENDLIST"
        for line in tail
    ):
        result.append("#EXT-X-ENDLIST")

    return "\n".join(result) + "\n"


def prepare_playlists(master_url, jwt, include_filler=False):
    master = load_playlist(master_url, jwt)

    video_url, audio_url, quality = select_variant(
        master, master_url
    )

    print(f"Selected quality: {quality}")

    queue = DownloadQueue()

    video = rewrite_playlist(
        (load_playlist(video_url, jwt) if include_filler else remove_trailing_filler(load_playlist(video_url, jwt), "video")),
        video_url, queue, "video"
    )

    audio = None

    if audio_url:
        print("Separate audio track detected.")

        audio = rewrite_playlist(
            (load_playlist(audio_url, jwt) if include_filler else remove_trailing_filler(load_playlist(audio_url, jwt), "audio")),
            audio_url, queue, "audio"
        )

    return queue, video, audio


# ---------------- Local FFmpeg merge ----------------

def merge(video, audio, output):
    print("\nMerging local media files...")

    partial = output.with_name(
        output.stem + ".partial.mp4"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-stats",
        "-y",
        "-protocol_whitelist", "file,crypto,data",
        "-allowed_extensions", "ALL",
        "-i", str(video)
    ]

    if audio:
        command += [
            "-protocol_whitelist", "file,crypto,data",
            "-allowed_extensions", "ALL",
            "-i", str(audio),
            "-map", "0:v:0",
            "-map", "1:a:0"
        ]
    else:
        command += [
            "-map", "0:v:0",
            "-map", "0:a:0?"
        ]

    command += [
        "-c", "copy",
        "-movflags", "+faststart",
        str(partial)
    ]

    try:
        run(command, capture=True)

        if not partial.exists() or partial.stat().st_size == 0:
            raise RuntimeError("FFmpeg generated an empty file.")

        partial.replace(output)

    except BaseException:
        partial.unlink(missing_ok=True)
        raise


# ---------------- Main ----------------

def main():
    args = parse_args()
    mode = get_cleanup_mode(args)


    success = False
    interrupted = False

    try:
        check_dependencies()

        if WORKDIR.exists():
            print(f"\nExisting workspace: {WORKDIR}")

        SEGDIR.mkdir(parents=True, exist_ok=True)

        try:
            os.chmod(WORKDIR, 0o700)
            os.chmod(SEGDIR, 0o700)
        except OSError:
            pass

        print("\nFetching episode metadata...")

        episode = metadata(args.url)

        print(f"Series: {episode['series']}")
        print(f"Season: {episode['season']}")
        print(f"Episode: {episode['episode']}")
        print(f"Title: {episode['title']}")

        output = choose_output(args, episode)
        if args.output:
            destination = Path(args.output).expanduser()
            destination.mkdir(parents=True, exist_ok=True)
            output = destination / output.name

        if output.exists():
            answer = prompt(
                "Output exists. Overwrite? [y/N]: "
            ).strip().lower()

            if answer != "y":
                raise RuntimeError("Cancelled.")

        print("\nInitializing session...")

        jwt = start_session()

        print("Requesting HLS stream...")

        master = streaming_url(episode["id"], jwt)

        queue, video, audio = prepare_playlists(
            master, jwt
        )

        queue.download(jwt, args.workers)

        merge(video, audio, output)

        print("\nDownload completed!")
        print(f"Output: {output}")
        print(
            f"File size: "
            f"{format_bytes(output.stat().st_size)}"
        )

        success = True

    except KeyboardInterrupt:
        interrupted = True
        print("\n\nDownload interrupted by user.")
        stop_processes()

    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        print(
            "Temporary files preserved for troubleshooting.",
            file=sys.stderr
        )

    finally:
        # Apply cleanup on success or Ctrl+C.
        # Keep files on unexpected failures for debugging.
        if success or interrupted:
            cleanup(mode)

    if interrupted:
        return 130

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

