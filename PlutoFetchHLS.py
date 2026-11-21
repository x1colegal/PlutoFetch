"""Local HLS relay: Pluto TV via HTTPX -> FFmpeg -> MPEG-TS stdout."""

import hashlib
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit


class IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True


class IPv4Server(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True


class HLSRelay:
    def __init__(self, p, video_url, audio_url, jwt):
        self.p = p
        self.video_url = video_url
        self.audio_url = audio_url
        self.jwt = jwt
        self.targets = {}
        self.lock = threading.Lock()

        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def send_data(self, data, kind):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path

                try:
                    if path == "/master.m3u8":
                        data = relay.master().encode("utf-8")
                        return self.send_data(
                            data, "application/vnd.apple.mpegurl"
                        )

                    if path == "/video.m3u8":
                        data = relay.playlist(
                            relay.video_url
                        ).encode("utf-8")
                        return self.send_data(
                            data, "application/vnd.apple.mpegurl"
                        )

                    if path == "/audio.m3u8" and relay.audio_url:
                        data = relay.playlist(
                            relay.audio_url
                        ).encode("utf-8")
                        return self.send_data(
                            data, "application/vnd.apple.mpegurl"
                        )

                    with relay.lock:
                        target = relay.targets.get(path)

                    if target is None:
                        self.send_error(404, "Unknown HLS resource")
                        return

                    url, kind = target

                    # HTTPX handles all remote media/key requests.
                    data = relay.p.curl(url, jwt=relay.jwt)

                    return self.send_data(data, kind)

                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:
                    print(
                        f"HLS relay error: {type(exc).__name__}: {exc}",
                        flush=True
                    )
                    try:
                        self.send_error(502, "HLS upstream failure")
                    except OSError:
                        pass

        try:
            self.server = IPv6Server(("::1", 0), Handler)
            self.host = "::1"
            self.base = f"http://[::1]:{self.server.server_port}"
        except OSError as exc:
            print(
                f"HLS IPv6 ::1 unavailable: {exc}; "
                "trying IPv4 fallback",
                flush=True
            )
            self.server = IPv4Server(("127.0.0.1", 0), Handler)
            self.host = "127.0.0.1"
            self.base = (
                f"http://127.0.0.1:{self.server.server_port}"
            )

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="pluto-hls-relay"
        )
        self.thread.start()

        print(
            "HLS HTTPX relay listening:",
            self.base,
            flush=True
        )

    def register(self, url, kind):
        digest = hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()

        suffix = ".key" if kind == "application/octet-stream" else ".ts"
        path = "/asset/" + digest + suffix

        with self.lock:
            self.targets[path] = (url, kind)

        return self.base + path

    def playlist(self, remote_url):
        text = self.p.load_playlist(
            remote_url, self.jwt
        )

        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")
        else:
            text = text.lstrip("\ufeff")

        result = []

        for line in text.splitlines():
            stripped = line.strip()

            if not stripped:
                result.append(line)
                continue

            if stripped.startswith("#"):
                # Preserve all HLS metadata, including:
                # DISCONTINUITY, MEDIA-SEQUENCE, KEY and EXTINF.
                if "URI=" in line:
                    def rewrite_uri(match):
                        absolute = urljoin(
                            remote_url, match.group(2)
                        )

                        kind = (
                            "application/octet-stream"
                            if stripped.startswith("#EXT-X-KEY:")
                            else "video/mp2t"
                        )

                        local = self.register(
                            absolute, kind
                        )

                        return (
                            match.group(1) + local + match.group(3)
                        )

                    line = re.sub(
                        r'(URI=")([^"]+)(")',
                        rewrite_uri,
                        line
                    )

                result.append(line)
                continue

            absolute = urljoin(remote_url, stripped)
            result.append(
                self.register(absolute, "video/mp2t")
            )

        return "\n".join(result) + "\n"

    def master(self):
        if not self.audio_url:
            return (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=5000000\n"
                f"{self.base}/video.m3u8\n"
            )

        return (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
            'NAME="Portuguese",LANGUAGE="pt",'
            'DEFAULT=YES,AUTOSELECT=YES,'
            f'URI="{self.base}/audio.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=5000000,'
            'AUDIO="audio"\n'
            f"{self.base}/video.m3u8\n"
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def run_hls(p, video_url, audio_url, jwt,
            server=None, output=None, live=True):

    relay = HLSRelay(
        p, video_url, audio_url, jwt
    )

    process = None
    reader = None
    bytes_out = [0]
    last_output = [time.monotonic()]
    started = [False]

    try:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-nostdin",
        ]

        # Pre-recorded VOD must be transmitted in real time.
        if not live:
            command += ["-re"]

        command += [
            "-fflags", "+genpts+discardcorrupt",
            "-dts_delta_threshold", "10",
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto,data",
            "-i", relay.base + "/master.m3u8",
        ]

        maps = [
            "-map", "0:v:0",
            "-map", "0:a:0?"
        ]

        command += maps + [
            "-c", "copy",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-mpegts_flags", "+resend_headers",
            "-flush_packets", "1",
            "-f", "mpegts",
            "pipe:1"
        ]

        if output is not None:
            command += maps + [
                "-c", "copy",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4",
                "-y", str(output)
            ]

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0
        )

        with p.RUNNING_LOCK:
            p.RUNNING.add(process)

        def forward():
            try:
                while True:
                    data = process.stdout.read(188 * 128)
                    if not data:
                        break

                    if not started[0]:
                        started[0] = True
                        print(
                            "FFmpeg HLS -> MPEG-TS stdout active",
                            flush=True
                        )

                    bytes_out[0] += len(data)
                    last_output[0] = time.monotonic()

                    if server is not None:
                        server.publish(data)

            except (OSError, ValueError) as exc:
                if not p.STOP.is_set():
                    print(
                        f"FFmpeg stdout error: {exc}",
                        flush=True
                    )

        reader = threading.Thread(
            target=forward,
            daemon=True,
            name="pluto-ffmpeg-stdout"
        )
        reader.start()

        print(
            "FFmpeg reading HLS playlist:",
            relay.base + "/master.m3u8",
            flush=True
        )

        last_warning = 0.0

        while not p.STOP.is_set():
            code = process.poll()

            if code is not None:
                reader.join(timeout=3)

                if code != 0:
                    raise RuntimeError(
                        f"FFmpeg HLS exited with code {code}"
                    )

                if live:
                    raise RuntimeError(
                        "FFmpeg HLS ended unexpectedly during Live TV"
                    )

                break

            now = time.monotonic()

            if now - last_output[0] >= 15:
                if now - last_warning >= 15:
                    print(
                        "FFmpeg HLS waiting for media: "
                        f"{now-last_output[0]:.1f}s "
                        "without MPEG-TS output",
                        flush=True
                    )
                    last_warning = now

            p.STOP.wait(0.25)

        return bytes_out[0]

    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

            if reader is not None:
                reader.join(timeout=3)

            if process.stdout is not None:
                process.stdout.close()

            with p.RUNNING_LOCK:
                p.RUNNING.discard(process)

        relay.close()
