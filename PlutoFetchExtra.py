"""Optional recording and paced VOD streaming for PlutoFetch.
No external remuxer for live recordings (fragmented MP4 writer).
"""
import datetime
import os
import re
import struct
import threading
import time
from pathlib import Path

import PlutoFetchLive as live


def box(t, payload=b''):
    return struct.pack('>I4s', len(payload)+8, t.encode('ascii')) + payload


def fullbox(t, version, flags, payload=b''):
    return box(t, bytes([version]) + flags.to_bytes(3, 'big') + payload)


def u16(x): return struct.pack('>H', x)
def u32(x): return struct.pack('>I', x)
def i32(x): return struct.pack('>i', x)
def u64(x): return struct.pack('>Q', x)


def h264_nals(blob):
    starts = [m for m in re.finditer(b'\x00\x00\x01', blob)]
    result = []
    for idx, m in enumerate(starts):
        offset = m.start()+3
        end = starts[idx+1].start() if idx+1 < len(starts) else len(blob)
        # When Annex B prefix is 00 00 00 01, the extra zero belongs to trailing bytes.
        payload = blob[offset:end].rstrip(b'\x00')
        if payload:
            result.append(payload)
    return result


def pes_content(pes):
    if len(pes) < 9 or pes[:3] != b'\x00\x00\x01':
        return None
    flags = pes[7]
    hlen = pes[8]
    def timestamp(b):
        return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)
    pts = timestamp(pes[9:14]) if flags & 0x80 and len(pes) >= 14 else None
    dts = timestamp(pes[14:19]) if flags & 0x40 and len(pes) >= 19 else pts
    return pts, dts, pes[9+hlen:]


def pes_packets(ts, pid):
    current = bytearray()
    for i in range(0, len(ts)-187, 188):
        pkt = ts[i:i+188]
        if pkt[0] != 0x47 or (pkt[1] & 0x80):
            continue
        if ((pkt[1] & 31) << 8 | pkt[2]) != pid:
            continue
        afc = (pkt[3] >> 4) & 3
        if afc not in (1,3):
            continue
        offset = 4 + (1 + pkt[4] if afc == 3 else 0)
        if offset >= 188:
            continue
        if pkt[1] & 0x40:
            if current:
                parsed = pes_content(bytes(current))
                if parsed:
                    yield parsed
            current.clear()
        current.extend(pkt[offset:])
    if current:
        parsed = pes_content(bytes(current))
        if parsed:
            yield parsed


def parse_adts(payload, pts):
    frames = []
    pos = 0
    while pos + 7 <= len(payload):
        if payload[pos] != 255 or payload[pos+1] & 0xf0 != 0xf0:
            pos += 1
            continue
        profile = (payload[pos+2] >> 6) & 3
        sr_index = (payload[pos+2] >> 2) & 15
        chan = ((payload[pos+2] & 1) << 2) | ((payload[pos+3] >> 6) & 3)
        length = ((payload[pos+3] & 3) << 11) | (payload[pos+4] << 3) | (payload[pos+5] >> 5)
        header_len = 7 if payload[pos+1] & 1 else 9
        if length < header_len or pos+length > len(payload) or sr_index >= 13:
            break
        sample_rate = (96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350)[sr_index]
        config = bytes([((profile+1) << 3) | (sr_index >> 1), ((sr_index & 1) << 7) | (chan << 3)])
        data = payload[pos+header_len:pos+length]
        timestamp = pts + len(frames) * (1024 * 90000 // sample_rate)
        frames.append((timestamp, data, config, sample_rate, chan))
        pos += length
    return frames


def desc(tag, content):
    # ISO 14496 descriptor variable-length encoded size
    size = len(content)
    encoded = [size & 127]
    size >>= 7
    while size:
        encoded.insert(0, (size & 127) | 128)
        size >>= 7
    return bytes([tag] + encoded) + content


def esds(config):
    decspecific = desc(5, config)
    decoder = desc(4, bytes.fromhex('40150000000000000000000000') + decspecific)
    es = desc(3, b'\x00\x01\x00' + decoder + desc(6, b'\x02'))
    return fullbox('esds',0,0,es)


def visual_sample_entry(sps, pps, width=1920, height=1080):
    avcc = bytes([1, sps[1], sps[2], sps[3], 255, 225]) + u16(len(sps)) + sps + bytes([1]) + u16(len(pps)) + pps
    base = b'\0'*6 + u16(1) + b'\0'*16 + u16(width) + u16(height) + u32(0x00480000)*2 + u32(0) + u16(1)
    base += bytes([0]) + b'\0'*31 + u16(0x18) + u16(0xffff)
    return box('avc1',base+box('avcC',avcc))


def audio_sample_entry(config, samplerate, channels):
    base = b'\0'*6 + u16(1) + b'\0'*8 + u16(channels) + u16(16) + u16(0) + u16(0) + u32(samplerate<<16)
    return box('mp4a',base+esds(config))


def trak(track_id, handler, entry, timescale):
    video = handler == 'vide'
    tkhd = fullbox('tkhd',0,7, u32(0)*2 + u32(track_id) + u32(0)*2 + b'\0'*8 + u16(0)*2 + u16(0 if video else 256) + u16(0) +
                   u32(65536)+u32(0)*2 + u32(0)+u32(65536)+u32(0)+u32(0)*2+u32(1073741824)+u32((1920 if video else 0)<<16)+u32((1080 if video else 0)<<16))
    mdhd = fullbox('mdhd',0,0,u32(0)*2+u32(timescale)+u32(0)+u16(0x55c4)+u16(0))
    hdlr = fullbox('hdlr',0,0,u32(0)+handler.encode()+b'\0'*12+(b'VideoHandler' if video else b'SoundHandler')+b'\0')
    media_header = fullbox('vmhd',0,1,b'\0'*8) if video else fullbox('smhd',0,0,b'\0'*4)
    url = fullbox('url ',0,1)
    dinf = box('dinf',fullbox('dref',0,0,u32(1)+url))
    stbl = box('stbl',fullbox('stsd',0,0,u32(1)+entry)+fullbox('stts',0,0,u32(0))+fullbox('stsc',0,0,u32(0))+fullbox('stsz',0,0,u32(0)+u32(0))+fullbox('stco',0,0,u32(0)))
    return box('trak',tkhd+box('mdia',mdhd+hdlr+box('minf',media_header+dinf+stbl)))


def initialization(sps, pps, acfg, rate, channels):
    ftyp=box('ftyp',b'isom'+u32(0x200)+b'isomiso6avc1mp41')
    mvhd = fullbox('mvhd',0,0,u32(0)*2+u32(1000)+u32(0)+u32(65536)+u16(256)+u16(0)+b'\0'*8+
                   u32(65536)+u32(0)*2+u32(0)+u32(65536)+u32(0)+u32(0)*2+u32(1073741824)+b'\0'*24+u32(3))
    tracks = trak(1,'vide',visual_sample_entry(sps,pps),90000)+trak(2,'soun',audio_sample_entry(acfg,rate,channels),90000)
    mvex=box('mvex',fullbox('trex',0,0,u32(1)+u32(1)+u32(0)*3)+fullbox('trex',0,0,u32(2)+u32(1)+u32(0)*3))
    return ftyp+box('moov',mvhd+tracks+mvex)


class MP4Writer:
    """Produces fragmented MP4 from H.264 Annex B / AAC ADTS in MPEG-TS blocks."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = self.path.open('wb')
        self.started = False
        self.sps = None
        self.pps = None
        self.audio_config = None
        self.rate = 48000
        self.channels = 2
        self.sequence = 1
        self.last_end = {1:0,2:0}
        self.first_timestamp = None

    def write(self, ts):
        programs = live.parse_program(ts)
        vid = next((pid for pid, typ in programs.items() if typ==0x1b),None)
        aud = next((pid for pid, typ in programs.items() if typ==0x0f),None)
        if vid is None or aud is None:
            raise RuntimeError('MP4 recording requires H.264 video and AAC ADTS audio in TS.')
        video=[]
        audio=[]
        for pts,dts,payload in pes_packets(ts,vid):
            if pts is None or dts is None:
                continue
            nals=h264_nals(payload)
            for nal in nals:
                typ=nal[0]&31
                if typ == 7: self.sps=nal
                if typ == 8: self.pps=nal
            samples=[nal for nal in nals if nal[0]&31 not in (9,7,8,12)]
            if not any(nal[0]&31 in (1,5) for nal in samples):
                continue
            video.append((dts, pts, b''.join(u32(len(nal))+nal for nal in samples), any(nal[0]&31==5 for nal in samples)))
        for pts,dts,payload in pes_packets(ts,aud):
            if pts is None:
                continue
            for stamp,data,cfg,rate,channels in parse_adts(payload,pts):
                self.audio_config=cfg
                self.rate=rate
                self.channels=channels
                audio.append((stamp,stamp,data,True))
        if not video or not audio:
            raise RuntimeError('MP4 segment contains no decodable video or audio frames.')
        if not self.started:
            if not self.sps or not self.pps or not self.audio_config:
                raise RuntimeError('MP4 stream is missing SPS, PPS or AAC configuration.')
            self.file.write(initialization(self.sps,self.pps,self.audio_config,self.rate,self.channels))
            self.started=True
        self._fragment(video,audio)

    def _fragment(self, video, audio):
        all_samples={1:video,2:audio}
        if self.first_timestamp is None:
            self.first_timestamp=min(video[0][0],audio[0][0])
        prepared={}
        for track,samples in all_samples.items():
            samples=sorted(samples,key=lambda x:x[0])
            shift=self.first_timestamp
            base=max(self.last_end[track],max(0,samples[0][0]-shift))
            entries=[]
            step=3000 if track==1 else max(1,round(1024*90000/self.rate))
            for index,(dts,pts,data,key) in enumerate(samples):
                duration=max(1,samples[index+1][0]-dts) if index+1<len(samples) else step
                flag=0x02000000 if key else 0x01010000
                entries.append((duration,len(data),flag,int(pts-dts),data))
            self.last_end[track]=base+sum(e[0] for e in entries)
            prepared[track]=(base,entries)
        def make_traf(track,offset):
            base,entries=prepared[track]
            samples=b''.join(u32(duration)+u32(length)+u32(flag)+i32(cts) for duration,length,flag,cts,_ in entries)
            return box('traf',fullbox('tfhd',0,0x020000,u32(track))+fullbox('tfdt',1,0,u64(base))+
                       fullbox('trun',1,0x000f01,u32(len(entries))+i32(offset)+samples))
        video_payload=b''.join(x[4] for x in prepared[1][1])
        audio_payload=b''.join(x[4] for x in prepared[2][1])
        def make_moof(vo,ao):
            return box('moof',fullbox('mfhd',0,0,u32(self.sequence))+make_traf(1,vo)+make_traf(2,ao))
        template=make_moof(0,0)
        v_offset=len(template)+8
        a_offset=v_offset+len(video_payload)
        moof=make_moof(v_offset,a_offset)
        self.file.write(moof)
        self.file.write(box('mdat',video_payload+audio_payload))
        self.file.flush()
        self.sequence+=1

    def close(self):
        if not self.file.closed:
            self.file.close()


def destination(p, args, suggested):
    directory=Path(args.output).expanduser() if args.output else p.HOME
    directory.mkdir(parents=True,exist_ok=True)
    name=p.filename(args.name or suggested)
    if name.lower().endswith('.mp4'):
        name=name[:-4]
    out=directory/(name+'.mp4')
    if out.exists() and p.prompt('Output exists. Overwrite? [y/N]: ').strip().lower() != 'y':
        raise RuntimeError('Cancelled.')
    return out


def _segments(p, video_url, audio_url, jwt, follow):
    from concurrent.futures import ThreadPoolExecutor
    from collections import deque

    INITIAL_BUFFER = 30.0
    MIN_AHEAD = 15.0
    MAX_QUEUED = 60.0
    POLL_INTERVAL = 0.65

    waiting = deque()
    seen = set()
    lock = threading.Condition()
    stop = threading.Event()
    finished = threading.Event()
    failures = []
    local = threading.local()

    executor = ThreadPoolExecutor(
        max_workers=10,
        thread_name_prefix='pluto-hls'
    )

    def fetch(item):
        if item is None:
            return None
        if not hasattr(local, 'keys'):
            local.keys = {}
        return live.fetch_segment(p, item, jwt, local.keys)

    def producer():
        last_new = time.monotonic()
        first = True
        last_queued_seq = None

        try:
            while not stop.is_set() and not p.STOP.is_set():

                vf = executor.submit(
                    p.load_playlist, video_url, jwt
                )
                af = executor.submit(
                    p.load_playlist, audio_url, jwt
                ) if audio_url else None

                vitems, period = live.playlist_items(
                    vf.result(), video_url
                )

                if not vitems:
                    raise RuntimeError(
                        'Empty HLS video playlist'
                    )

                if af is not None:
                    aitems, _ = live.playlist_items(
                        af.result(), audio_url
                    )
                    amap = {
                        a['seq']: a for a in aitems
                    }
                    pairs = [
                        (v, amap[v['seq']])
                        for v in vitems
                        if v['seq'] in amap
                    ]
                else:
                    pairs = [(v, None) for v in vitems]

                if first and follow:
                    initial = []
                    seconds = 0.0

                    for pair in reversed(pairs):
                        initial.append(pair)
                        seconds += pair[0]['duration']

                        if seconds >= INITIAL_BUFFER + MIN_AHEAD:
                            break

                    pairs = list(reversed(initial))

                first = False
                added = False

                for item, audio in pairs:
                    marker = (item['seq'], item['url'])
                    if last_queued_seq is not None and item["seq"] <= last_queued_seq:
                        continue

                    with lock:
                        queued = sum(
                            entry[0]['duration']
                            for entry in waiting
                        )

                        if queued >= MAX_QUEUED:
                            break

                        if marker in seen:
                            continue

                        seen.add(marker)
                        last_queued_seq = item["seq"]

                        video_future = executor.submit(
                            fetch, item
                        )
                        audio_future = (
                            executor.submit(fetch, audio)
                            if audio else None
                        )

                        waiting.append((
                            item,
                            video_future,
                            audio_future
                        ))

                        lock.notify_all()

                    added = True

                if added:
                    last_new = time.monotonic()

                if not follow:
                    if len(seen) >= len(pairs):
                        return

                    stop.wait(.15)
                    continue

                if time.monotonic() - last_new > 60:
                    raise RuntimeError(
                        'No new live segments for 60 seconds'
                    )

                stop.wait(POLL_INTERVAL)

        except Exception as exc:
            failures.append(exc)

        finally:
            finished.set()
            with lock:
                lock.notify_all()

    thread = threading.Thread(
        target=producer,
        name='pluto-playlist',
        daemon=True
    )
    thread.start()

    started = False
    deadline = None
    buffer_started = time.monotonic()
    last_media = time.monotonic()
    last_warning = 0.0

    try:
        while not p.STOP.is_set():

            with lock:
                if failures:
                    raise failures[0]

                ready = []
                total = 0.0

                for entry in waiting:
                    video_done = entry[1].done()
                    audio_done = (
                        entry[2] is None
                        or entry[2].done()
                    )

                    if not video_done or not audio_done:
                        break

                    ready.append(entry)
                    total += entry[0]['duration']

                if not ready:
                    if finished.is_set() and not waiting:
                        return

                    now = time.monotonic()
                    if now - last_media >= 8 and now - last_warning >= 8:
                        print(
                            f"HLS waiting: {len(waiting)} queued, "
                            f"{len(seen)} scheduled, "
                            f"{now-last_media:.1f}s without delivery",
                            flush=True
                        )
                        last_warning = now

                    lock.wait(timeout=.10)
                    continue

                head = ready[0]
                reserve = (
                    total - head[0]['duration']
                )

                # Initial buffering is a target, not a permanent gate.
                # Do not deadlock when the live HLS window is too short.
                if not started and not finished.is_set():
                    elapsed = time.monotonic() - buffer_started
                    if total < INITIAL_BUFFER and (
                        elapsed < 8.0 or total < 5.0
                    ):
                        lock.wait(timeout=.10)
                        continue

                # After startup, never hold a completed segment solely
                # to maintain MIN_AHEAD. That would stall the player
                # whenever the origin publishes segments late.
                waiting.popleft()
                started = True
                last_media = time.monotonic()

            item, video_future, audio_future = head

            video = video_future.result()
            audio = (
                audio_future.result()
                if audio_future else None
            )

            if follow:
                yield item, video, audio
                continue
            if deadline is None:
                deadline = time.monotonic() - 10.0

            delay = deadline - time.monotonic()

            if delay > 0 and p.STOP.wait(delay):
                return

            if p.STOP.is_set():
                return

            yield item, video, audio

            deadline = max(
                deadline + item['duration'],
                time.monotonic()
            )

    finally:
        stop.set()
        executor.shutdown(
            wait=False,
            cancel_futures=True
        )


def record_channel(p, channel_id, jwt, args):
    from PlutoFetchHLS import run_hls
    from urllib.parse import urljoin

    url = urljoin(
        p.PLAYOUT_BASE,
        f"v2/stitch/hls/channel/{channel_id}/master.m3u8"
    )

    video_url, audio_url, quality = p.select_variant(
        p.load_playlist(url, jwt), url
    )

    print("Selected quality:", quality)

    out = destination(
        p, args,
        f"PlutoTV - {channel_id} - "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H-%M-%S')}"
    )

    server = (
        None if args.record_only
        else live.Broadcaster(
            args.listen_on_ip_family,
            args.port
        )
    )

    try:
        run_hls(
            p,
            video_url,
            audio_url,
            jwt,
            server=server,
            output=out,
            live=True,
            refresh=lambda: p.select_variant(
                p.load_playlist(url, jwt), url
            )[:2]
        )
    finally:
        if server is not None:
            server.close()

        print("Recording:", out)





class PerClientVODServer(live.Broadcaster):
    """One independent HLS/FFmpeg playback for each TCP client."""

    def __init__(self, p, video_url, audio_url,
                 jwt, family, port):
        self.p = p
        self.video_url = video_url
        self.audio_url = audio_url
        self.jwt = jwt
        self.sessions = {}
        super().__init__(family, port)

    def _accept(self, listener):
        import socket

        while (
            not self.stopped.is_set()
            and not self.p.STOP.is_set()
        ):
            try:
                client, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client.settimeout(5.0)
            stopped = threading.Event()

            with self.lock:
                self.clients[client] = None
                self.sessions[client] = stopped
                count = len(self.clients)

            print(
                f"[Live VOD] Client connected: {addr[0]} | "
                f"clients {count} | starting at 00:00",
                flush=True
            )

            threading.Thread(
                target=self._play_client,
                args=(client, stopped),
                daemon=True,
                name="pluto-vod-client"
            ).start()

    def _play_client(self, client, stopped):
        import select
        import socket
        from PlutoFetchHLS import run_hls

        parent = self

        class ClientOutput:
            def __init__(self):
                self.lock = threading.Lock()
                self.clients = {client: None}

            def publish(self, data):
                try:
                    client.sendall(data)
                except (OSError, ValueError):
                    stopped.set()
                    raise

        def watch_disconnect():
            while (
                not stopped.wait(0.5)
                and not parent.p.STOP.is_set()
            ):
                try:
                    ready, _, _ = select.select(
                        [client], [], [], 0
                    )

                    if ready:
                        data = client.recv(
                            1, socket.MSG_PEEK
                        )
                        if not data:
                            stopped.set()
                            return

                except (OSError, ValueError):
                    stopped.set()
                    return

        watcher = threading.Thread(
            target=watch_disconnect,
            daemon=True,
            name="pluto-vod-disconnect"
        )
        watcher.start()

        try:
            run_hls(
                self.p,
                self.video_url,
                self.audio_url,
                self.jwt,
                server=ClientOutput(),
                live=False,
                stop_event=stopped
            )

        except Exception as exc:
            if not stopped.is_set() and not self.p.STOP.is_set():
                print(
                    f"[Live VOD] Client playback failed: {exc}",
                    flush=True
                )

        finally:
            stopped.set()

            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                client.close()
            except OSError:
                pass

            with self.lock:
                self.clients.pop(client, None)
                self.sessions.pop(client, None)
                count = len(self.clients)

            print(
                f"[Live VOD] Client finished | "
                f"clients {count}",
                flush=True
            )

    def close(self):
        if hasattr(self, "sessions"):
            with self.lock:
                sessions = list(self.sessions.values())

            for stopped in sessions:
                stopped.set()

        super().close()


def vod_stream_independent(
    p, video_url, audio_url, jwt, args, download
):
    server = PerClientVODServer(
        p, video_url, audio_url, jwt,
        args.listen_on_ip_family,
        args.port
    )

    errors = []

    if download is not None:
        def download_task():
            try:
                download()
            except Exception as exc:
                errors.append(exc)

        threading.Thread(
            target=download_task,
            daemon=True,
            name="pluto-vod-download"
        ).start()

    print(
        "[Live VOD] Per-client playback enabled. "
        "Each TCP connection starts at 00:00.",
        flush=True
    )

    try:
        while not p.STOP.wait(0.5):
            if errors:
                raise errors[0]
    finally:
        server.close()


def vod_stream(p, master, jwt, args, episode, download=None):
    from PlutoFetchHLS import run_hls

    video_url, audio_url, quality = p.select_variant(
        p.load_playlist(master, jwt), master
    )

    print("Selected quality:", quality)

    if getattr(args, "live_vod_per_client", False):
        return vod_stream_independent(
            p, video, audio, jwt, args, download
        )

    server = live.Broadcaster(
        args.listen_on_ip_family,
        args.port
    )

    errors = []
    worker = None

    if download is not None:
        def task():
            try:
                download()
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(
            target=task,
            daemon=True
        )
        worker.start()

    try:
        print("Live VOD: waiting for TCP client...", flush=True)

        while not p.STOP.is_set():
            with server.lock:
                if server.clients:
                    break
            p.STOP.wait(0.2)

        if p.STOP.is_set():
            return

        run_hls(
            p,
            video_url,
            audio_url,
            jwt,
            server=server,
            live=False
        )

        if worker is not None:
            worker.join()

        if errors:
            raise errors[0]

    finally:
        server.close()

