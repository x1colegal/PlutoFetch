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
            server=None, output=None, live=True,
            refresh=None):
    """
    HTTPX HLS relay -> FFmpeg -> stdout -> local TCP.

    Live mode supervises the FFmpeg process and recovers from
    stalled output, stalled media timestamps or unexpected exit.
    """
    attempt = 0
    total_bytes = 0

    while not p.STOP.is_set():
        attempt += 1

        if attempt > 1 and refresh is not None:
            try:
                video_url, audio_url = refresh()
                print(
                    "[Recovery] Pluto HLS URLs refreshed",
                    flush=True
                )
            except Exception as exc:
                print(
                    f"[Recovery] Playlist refresh failed: {exc}",
                    flush=True
                )
                if p.STOP.wait(3):
                    break
                continue

        relay = HLSRelay(p, video_url, audio_url, jwt)
        process = None
        threads = []

        state = {
            "bytes": 0,
            "last_bytes": time.monotonic(),
            "last_progress": time.monotonic(),
            "media_us": None,
            "progress_seen": False,
            "finished": False,
            "error": None,
        }

        started_at = time.monotonic()
        restart_reason = None

        try:
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "repeat+level+info",
                "-nostats",
                "-stats_period", "2",
                "-progress", "pipe:2",
                "-nostdin",
            ]

            if not live:
                command += ["-re"]

            command += [
                "-rw_timeout", "10000000",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "429,500,502,503,504",
                "-reconnect_delay_max", "2",
                "-max_reload", "8",
                "-m3u8_hold_counters", "8",
                "-live_start_index", "-3",
                "-fflags", "+genpts+discardcorrupt",
                "-dts_delta_threshold", "10",
                "-protocol_whitelist",
                "file,http,https,tcp,tls,crypto,data",
                "-i", relay.base + "/master.m3u8",
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c", "copy",
                "-muxdelay", "0",
                "-muxpreload", "0",
                "-mpegts_flags", "+resend_headers",
                "-flush_packets", "1",
                "-f", "mpegts",
                "pipe:1",
            ]

            if output is not None:
                from pathlib import Path
                out = Path(output)

                if attempt > 1:
                    out = out.with_name(
                        out.stem + f".restart{attempt-1}" + out.suffix
                    )
                    print(
                        f"[Recording] New MP4 part: {out}",
                        flush=True
                    )

                command += [
                    "-map", "0:v:0",
                    "-map", "0:a:0?",
                    "-c", "copy",
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof",
                    "-f", "mp4",
                    "-y", str(out)
                ]

            print(
                f"[FFmpeg] Starting pipeline #{attempt}",
                flush=True
            )

            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            with p.RUNNING_LOCK:
                p.RUNNING.add(process)

            def forward():
                try:
                    while True:
                        data = process.stdout.read(188 * 128)
                        if not data:
                            break

                        state["bytes"] += len(data)
                        state["last_bytes"] = time.monotonic()

                        if server is not None:
                            server.publish(data)

                except (OSError, ValueError) as exc:
                    state["error"] = str(exc)

                finally:
                    state["finished"] = True

            def ffmpeg_logs():
                try:
                    for raw in process.stderr:
                        line = raw.decode(
                            "utf-8", errors="replace"
                        ).strip()

                        if not line:
                            continue

                        if line.startswith("out_time_us="):
                            try:
                                value = int(line.split("=", 1)[1])
                            except ValueError:
                                continue

                            old = state["media_us"]

                            if old is None or value > old:
                                state["last_progress"] = (
                                    time.monotonic()
                                )

                            state["media_us"] = value
                            state["progress_seen"] = True
                            continue

                        if line.startswith("out_time_ms="):
                            if state["progress_seen"]:
                                continue
                            try:
                                value = int(line.split("=", 1)[1])
                            except ValueError:
                                continue
                            old = state["media_us"]
                            if old is None or value > old:
                                state["last_progress"] = (
                                    time.monotonic()
                                )
                            state["media_us"] = value
                            continue

                        if line.startswith((
                            "frame=",
                            "fps=",
                            "stream_",
                            "bitrate=",
                            "total_size=",
                            "out_time=",
                            "dup_frames=",
                            "drop_frames=",
                            "speed=",
                            "progress=",
                        )):
                            continue

                        print("[FFmpeg] " + line, flush=True)

                except (OSError, ValueError) as exc:
                    if not p.STOP.is_set():
                        print(
                            f"[FFmpeg stderr] {exc}",
                            flush=True
                        )

            for target, name in (
                (forward, "pluto-ts-forward"),
                (ffmpeg_logs, "pluto-ffmpeg-logs")
            ):
                thread = threading.Thread(
                    target=target,
                    name=name,
                    daemon=True
                )
                thread.start()
                threads.append(thread)

            last_report = 0.0

            while not p.STOP.is_set():
                now = time.monotonic()
                code = process.poll()

                if now - last_report >= 5:
                    last_report = now

                    clients = 0
                    if server is not None:
                        with server.lock:
                            clients = len(server.clients)

                    media = state["media_us"]
                    media_text = (
                        f"{media / 1000000:.1f}s"
                        if media is not None else "--"
                    )

                    print(
                        f"[Live] clients {clients} | "
                        f"TS {state['bytes']/1048576:.2f} MB | "
                        f"media {media_text} | "
                        f"no output "
                        f"{now-state['last_bytes']:.1f}s | "
                        f"FFmpeg #{attempt}",
                        flush=True
                    )

                if code is not None:
                    if not live and code == 0:
                        restart_reason = None
                    else:
                        restart_reason = (
                            f"FFmpeg exited (code {code})"
                        )
                    break

                startup = now - started_at

                if state["bytes"] == 0:
                    if startup >= 35:
                        restart_reason = (
                            "FFmpeg startup timeout (35s)"
                        )
                        break
                else:
                    if now - state["last_bytes"] >= 20:
                        restart_reason = (
                            "MPEG-TS output stalled for 20s"
                        )
                        break

                    if (
                        state["progress_seen"]
                        and now - state["last_progress"] >= 20
                    ):
                        restart_reason = (
                            "FFmpeg media clock stalled for 20s"
                        )
                        break

                if state["error"]:
                    restart_reason = (
                        "stdout failure: " + state["error"]
                    )
                    break

                p.STOP.wait(0.25)

            total_bytes += state["bytes"]

        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()

                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

                for thread in threads:
                    thread.join(timeout=3)

                process.stdout.close()
                process.stderr.close()

                with p.RUNNING_LOCK:
                    p.RUNNING.discard(process)

            relay.close()

        if p.STOP.is_set():
            break

        if restart_reason is None:
            if live:
                restart_reason = "Live FFmpeg ended"
            else:
                return total_bytes

        print(
            f"[Recovery] {restart_reason}",
            flush=True
        )

        if not live:
            raise RuntimeError(restart_reason)

        # Reset the player's demuxer. Keep TCP listeners alive.
        if server is not None:
            server.close_clients()

        print(
            "[Recovery] Restarting FFmpeg and HLS relay",
            flush=True
        )

        if p.STOP.wait(2):
            break

    return total_bytes

