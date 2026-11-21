"""Live HLS to local multi-client MPEG-TS transport for PlutoFetch.

Uses curl through the parent application; no browser and no FFmpeg.
MPEG-TS packet remultiplexing supports H.264 and AAC in MPEG-TS inputs.
"""
import concurrent.futures
import hashlib
import json
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlparse

TS_SIZE = 188


def pick(prompt, entries, label):
    if not entries:
        raise RuntimeError('No matching results.')
    for i, item in enumerate(entries, 1):
        print(f'{i:3d}. {label(item)}')
    while True:
        choice = prompt(f'Select [1-{len(entries)}]: ').strip()
        if choice.isdecimal() and 1 <= int(choice) <= len(entries):
            return entries[int(choice) - 1]
        print('Invalid selection.')


def catalog_json(p, url):
    # Try the public catalog first; fall back to session-authenticated access.
    try:
        return json.loads(p.curl(url))
    except RuntimeError:
        jwt = getattr(p, 'ACTIVE_JWT', None)
        if not jwt:
            raise
        return json.loads(p.curl(url, jwt=jwt))


def search_channels(p, query):
    from urllib.parse import quote
    urls = [
        'https://service-channels.clusters.pluto.tv/v2/guide/channels?offset=0&limit=1000&sort=number:asc',
        'https://api.pluto.tv/v2/channels.json',
    ]
    errors = []
    channels = []
    for url in urls:
        try:
            payload = catalog_json(p, url)
            batch = payload.get('data', []) if isinstance(payload, dict) else payload
            if isinstance(batch, list):
                channels.extend(batch)
        except Exception as exc:
            errors.append(str(exc))
    if not isinstance(channels, list) or not channels:
        raise RuntimeError('Channel catalog unavailable: ' + '; '.join(errors))
    query = query.casefold()
    results = []
    seen = set()
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get('name') or channel.get('title') or '')
        cid = channel.get('id') or channel.get('_id')
        if not cid or query not in name.casefold() or cid in seen:
            continue
        seen.add(cid)
        results.append({'id': cid, 'name': name, 'number': channel.get('number', '')})
    return results


def search_series(p, query):
    # Search the currently published regional VOD catalog. Category trees differ
    # across Pluto deployments, so traverse nested items rather than hardcode a category.
    payload = catalog_json(p, 'https://api.pluto.tv/v3/vod/categories?includeItems=true&deviceType=web')
    results, seen = [], set()
    def visit(obj):
        if isinstance(obj, list):
            for item in obj:
                visit(item)
        elif isinstance(obj, dict):
            kind = str(obj.get('type') or obj.get('contentType') or '').lower()
            title = str(obj.get('name') or obj.get('title') or '')
            ident = obj.get('_id') or obj.get('id')
            if ident and query.casefold() in title.casefold() and (
                kind in ('series', 'show', 'tvshow') or
                (obj.get('seasons') is not None) or
                (obj.get('slug') and 'series' in str(obj.get('slug', '')).lower())
            ):
                if str(ident) not in seen:
                    seen.add(str(ident))
                    results.append({'id': str(ident), 'title': title, 'slug': obj.get('slug')})
            for key in ('data', 'items', 'categories', 'children', 'shows', 'series', 'content'):
                if key in obj:
                    visit(obj[key])
    visit(payload)
    return results


def resolve_series_id(p, public_id):
    if re.fullmatch(r"[0-9a-fA-F]{24}", public_id):
        return public_id
    url = "https://pluto.tv/br/shows/" + public_id + "/"
    final_url = p.http_final_url(url)
    slug = urlparse(final_url).path.rstrip("/").split("/")[-1]
    payload = catalog_json(p, "https://api.pluto.tv/v3/vod/categories?includeItems=true\&deviceType=web".replace("\\&", "&").replace("\&", "&"))
    matches = set()
    def visit(obj):
        if isinstance(obj, list):
            for value in obj:
                visit(value)
        elif isinstance(obj, dict):
            ident = str(obj.get("_id") or obj.get("id") or "")
            item_slug = str(obj.get("slug") or "").strip("/").split("/")[-1]
            if re.fullmatch(r"[0-9a-fA-F]{24}", ident) and (item_slug.casefold() == slug.casefold() or str(obj.get("seriesID") or "") == public_id):
                matches.add(ident)
            for value in obj.values():
                if isinstance(value, (list, dict)):
                    visit(value)
    visit(payload)
    if len(matches) != 1:
        raise RuntimeError(f"Could not uniquely resolve series {public_id} (slug: {slug}, matches: {len(matches)}).")
    return matches.pop()


def select_episode(p, query=None, series_id=None):
    series = {'id': resolve_series_id(p, str(series_id))} if series_id is not None else pick(p.prompt, search_series(p, query), lambda x: x['title'])
    url = f"https://api.pluto.tv/v3/vod/series/{series['id']}/seasons?includeItems=true&deviceType=web"
    payload = catalog_json(p, url)
    if isinstance(payload, dict):
        seasons = payload.get('seasons') or payload.get('data') or payload.get('items') or []
    else:
        seasons = payload
    if isinstance(seasons, dict):
        seasons = seasons.get('items') or seasons.get('seasons') or []
    season_items = []
    for n, item in enumerate(seasons):
        if not isinstance(item, dict):
            continue
        episodes = item.get('episodes') or item.get('items') or []
        if isinstance(episodes, dict):
            episodes = episodes.get('items') or []
        if episodes:
            season_items.append({'number': item.get('number', item.get('seasonNumber', n+1)), 'episodes': episodes})
    season = pick(p.prompt, season_items, lambda x: f"Season {x['number']}")
    candidates = []
    for idx, ep in enumerate(season['episodes']):
        if not isinstance(ep, dict):
            continue
        ident = ep.get('_id') or ep.get('id')
        if not ident:
            continue
        candidates.append({'id': str(ident), 'number': ep.get('number', ep.get('episodeNumber', idx+1)), 'title': ep.get('name') or ep.get('title') or 'Episode'})
    episode = pick(p.prompt, candidates, lambda e: f"Episode {e['number']} - {e['title']}")
    return f"https://pluto.tv/br/shows/{series['id']}/episode/{episode['id']}/"


def playlist_items(text, base):
    lines = text.splitlines()
    media_seq = 0
    target = 5.0
    items = []
    key = None
    pending_duration = None
    program_date = None
    discont = False
    init_map = None
    for line in lines:
        line = line.strip()
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            media_seq = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-TARGETDURATION:'):
            target = float(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-KEY:'):
            attrs = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(':', 1)[1]))
            attrs = {k:v.strip('"') for k,v in attrs.items()}
            method = attrs.get('METHOD', 'NONE')
            if method not in ('NONE', 'AES-128') or attrs.get('KEYFORMAT', 'identity') != 'identity':
                raise RuntimeError('Unsupported encryption in live playlist: ' + method)
            key = {'method': method, 'url': urljoin(base, attrs['URI']) if 'URI' in attrs else None, 'iv': attrs.get('IV')}
        elif line.startswith('#EXT-X-MAP:'):
            raise RuntimeError('Fragmented MP4 live HLS is not supported; MPEG-TS is required.')
        elif line.startswith('#EXT-X-BYTERANGE:'):
            raise RuntimeError('Byte-range live playlists are not supported.')
        elif line.startswith('#EXT-X-DISCONTINUITY'):
            discont = True
        elif line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            program_date = line.split(':', 1)[1]
        elif line.startswith('#EXTINF:'):
            pending_duration = float(line.split(':', 1)[1].split(',', 1)[0])
        elif line and not line.startswith('#'):
            items.append({'seq': media_seq + len(items), 'url': urljoin(base, line), 'duration': pending_duration or target, 'key': key, 'discontinuity': discont, 'time': program_date})
            discont = False
            program_date = None
            pending_duration = None
    return items, target


def fetch_segment(p, item, jwt, keys):
    blob = None
    for attempt in range(8):
        if p.STOP.is_set():
            raise KeyboardInterrupt
        try:
            blob = p.curl(item['url'], jwt=jwt)
            break
        except RuntimeError as exc:
            if attempt == 7:
                raise
            delay = min(0.5 * (attempt + 1), 3.0)
            print(
                f"HLS segment {item['seq']}: download failed "
                f"(attempt {attempt + 1}/8): {exc}",
                flush=True
            )
            if p.STOP.wait(delay):
                raise KeyboardInterrupt
    key = item.get('key')
    if key and key['method'] == 'AES-128':
        if not shutil.which('openssl'):
            raise RuntimeError('AES-128 HLS requires openssl (not FFmpeg).')
        uri = key['url']
        if uri not in keys:
            keys[uri] = p.curl(uri, jwt=jwt)
        raw_key = keys[uri]
        if len(raw_key) != 16:
            raise RuntimeError('Invalid AES-128 key size.')
        iv = bytes.fromhex(key['iv'][2:]) if key.get('iv') else item['seq'].to_bytes(16, 'big')
        proc = subprocess.run(['openssl','enc','-d','-aes-128-cbc','-K',raw_key.hex(),'-iv',iv.hex()], input=blob, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode:
            raise RuntimeError('AES-128 decryption failed: ' + proc.stderr.decode(errors='replace'))
        blob = proc.stdout
    if len(blob) % TS_SIZE or blob[:1] != b'G':
        raise RuntimeError('Input segment is not an MPEG-TS packet stream.')
    return blob


def crc32_mpeg(data):
    crc = 0xffffffff
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04c11db7 if crc & 0x80000000 else 0)) & 0xffffffff
    return crc


def psi_packet(pid, section, counter=0):
    data = b'\x00' + section
    return (bytes([0x47, 0x40 | (pid >> 8), pid & 255, 0x10 | (counter & 15)]) + data).ljust(188, b'\xff')


def tables(video_type, audio_type):
    pat = bytes.fromhex('00b00d0001c100000001f000')
    pat += crc32_mpeg(pat).to_bytes(4, 'big')
    entries = [(video_type, 0x100), (audio_type, 0x101)] if audio_type is not None else [(video_type, 0x100)]
    body = bytearray([0x02, 0xb0, 0, 0x00, 0x01, 0xc1, 0x00, 0x00, 0xe1, 0x00, 0xf0, 0x00])
    for typ, pid in entries:
        body.extend([typ, 0xe0 | (pid >> 8), pid & 255, 0xf0, 0x00])
    length = len(body) - 3 + 4
    body[1] = 0xb0 | ((length >> 8) & 0x0f)
    body[2] = length & 255
    pmt = bytes(body)
    pmt += crc32_mpeg(pmt).to_bytes(4, 'big')
    return psi_packet(0, pat), psi_packet(0x1000, pmt)


def extract_section(pkt):
    adaptation = (pkt[3] >> 4) & 3
    if adaptation not in (1, 3):
        return b''
    offset = 4 + (1 + pkt[4] if adaptation == 3 else 0)
    if offset >= 188:
        return b''
    return pkt[offset+1+pkt[offset]:]


def parse_program(ts):
    pmt_pid = None
    streams = {}
    for i in range(0, len(ts), 188):
        pkt = ts[i:i+188]
        if len(pkt) != 188 or pkt[0] != 0x47 or not (pkt[1] & 0x40):
            continue
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid == 0:
            section = extract_section(pkt)
            if len(section) >= 12 and section[0] == 0:
                pmt_pid = ((section[10] & 0x1f) << 8) | section[11]
        elif pid == pmt_pid:
            section = extract_section(pkt)
            if len(section) >= 16 and section[0] == 2:
                start = 12 + (((section[10] & 15) << 8) | section[11])
                end = 3 + (((section[1] & 15) << 8) | section[2]) - 4
                pos = start
                while pos + 5 <= end:
                    typ = section[pos]
                    stream_pid = ((section[pos+1] & 31) << 8) | section[pos+2]
                    eslen = ((section[pos+3] & 15) << 8) | section[pos+4]
                    streams[stream_pid] = typ
                    pos += 5 + eslen
                if streams:
                    break
    return streams


def pts_at_packet(pkt):
    if not (pkt[1] & 0x40):
        return None
    afc = (pkt[3] >> 4) & 3
    if afc not in (1, 3):
        return None
    start = 4 + (1 + pkt[4] if afc == 3 else 0)
    pes = pkt[start:]
    if len(pes) < 14 or pes[:3] != b'\x00\x00\x01' or not (pes[7] & 0x80):
        return None
    b = pes[9:14]
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def dts_at_packet(pkt):
    if not (pkt[1] & 0x40):
        return None
    afc = (pkt[3] >> 4) & 3
    if afc not in (1, 3):
        return None
    start = 4 + (1 + pkt[4] if afc == 3 else 0)
    pes = pkt[start:]
    if len(pes) >= 19 and pes[:3] == b"\x00\x00\x01" and (pes[7] & 0xc0) == 0xc0:
        b = pes[14:19]
        return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)
    return pts_at_packet(pkt)

def pes_groups(ts, pid):
    groups = []
    current = []
    current_pts = None
    for i in range(0, len(ts), TS_SIZE):
        pkt = ts[i:i+TS_SIZE]
        if len(pkt) != TS_SIZE or pkt[0] != 0x47:
            continue
        if (((pkt[1] & 31) << 8) | pkt[2]) != pid:
            continue
        if pkt[1] & 0x40 and current:
            groups.append((current_pts, current))
            current = []
        pts = dts_at_packet(pkt)
        if pts is not None:
            current_pts = pts
        current.append(pkt)
    if current:
        groups.append((current_pts, current))
    return groups



def _rebase_ts(self, data, discontinuity=False):
    """Normalize PCR/PTS/DTS across HLS ad discontinuities."""
    data = bytearray(data)
    mask = (1 << 33) - 1
    half = 1 << 32

    def signed_delta(a, b):
        return ((a - b + half) & mask) - half

    def read_ts(pos):
        x = data[pos:pos+5]
        return (((x[0] >> 1) & 7) << 30) | (x[1] << 22) | ((x[2] >> 1) << 15) | (x[3] << 7) | (x[4] >> 1)

    def write_ts(pos, value):
        value &= mask
        data[pos] = (data[pos] & 0xf0) | (((value >> 30) & 7) << 1) | 1
        data[pos+1] = (value >> 22) & 255
        data[pos+2] = (((value >> 15) & 127) << 1) | 1
        data[pos+3] = (value >> 7) & 255
        data[pos+4] = ((value & 127) << 1) | 1

    def read_pcr(pos):
        return (
            (data[pos] << 25) |
            (data[pos+1] << 17) |
            (data[pos+2] << 9) |
            (data[pos+3] << 1) |
            (data[pos+4] >> 7)
        )

    def write_pcr(pos, value):
        value &= mask
        data[pos] = (value >> 25) & 255
        data[pos+1] = (value >> 17) & 255
        data[pos+2] = (value >> 9) & 255
        data[pos+3] = (value >> 1) & 255
        data[pos+4] = (data[pos+4] & 127) | ((value & 1) << 7)

    pcr_values = []
    pes_values = []

    for i in range(0, len(data) - 187, 188):
        if data[i] != 0x47:
            continue

        afc = (data[i+3] >> 4) & 3

        if (
            afc in (2, 3)
            and data[i+4] >= 7
            and data[i+5] & 0x10
        ):
            pcr_values.append(read_pcr(i+6))

        if not data[i+1] & 0x40 or afc not in (1, 3):
            continue

        pos = i + 4 + (1 + data[i+4] if afc == 3 else 0)

        if (
            pos + 14 > i + 188
            or data[pos:pos+3] != b"\\x00\\x00\\x01"
        ):
            continue

        flags = data[pos+7]

        if flags & 0x40 and pos + 19 <= i + 188:
            pes_values.append(read_ts(pos+14))
        elif flags & 0x80:
            pes_values.append(read_ts(pos+9))

    values = pcr_values or pes_values

    if not values:
        return bytes(data)

    first_raw = values[0]
    last_raw = values[-1]

    previous_raw = getattr(self, "_last_raw_clock", None)
    previous_output = getattr(self, "_last_output_clock", None)
    offset = getattr(self, "_clock_offset", None)

    if offset is None:
        offset = (90000 - first_raw) & mask

    elif previous_raw is not None and previous_output is not None:
        jump = signed_delta(first_raw, previous_raw)

        if discontinuity or jump < -90000 or jump > 900000:
            # Re-anchor the new HLS timeline just after the previous
            # segment instead of exposing a timestamp reset to OBS.
            next_output = (previous_output + 9000) & mask
            offset = (next_output - first_raw) & mask

            print(
                "HLS clock discontinuity: "
                f"raw jump {jump / 90000:.3f}s; "
                "PCR/PTS/DTS normalized",
                flush=True
            )

    for i in range(0, len(data) - 187, 188):
        if data[i] != 0x47:
            continue

        afc = (data[i+3] >> 4) & 3

        if (
            afc in (2, 3)
            and data[i+4] >= 7
            and data[i+5] & 0x10
        ):
            pos = i + 6
            write_pcr(pos, read_pcr(pos) + offset)

        if not data[i+1] & 0x40 or afc not in (1, 3):
            continue

        pos = i + 4 + (1 + data[i+4] if afc == 3 else 0)

        if (
            pos + 14 > i + 188
            or data[pos:pos+3] != b"\\x00\\x00\\x01"
        ):
            continue

        flags = data[pos+7]

        if flags & 0x80:
            write_ts(pos+9, read_ts(pos+9) + offset)

        if flags & 0x40 and pos + 19 <= i + 188:
            write_ts(pos+14, read_ts(pos+14) + offset)

    self._clock_offset = offset
    self._last_raw_clock = last_raw
    self._last_output_clock = (last_raw + offset) & mask

    return bytes(data)



class TsMuxer:
    def __init__(self):
        self.counters = {0x100: 0, 0x101: 0}

    def mux(self, video, audio=None, discontinuity=False):
        vs = parse_program(video)
        vp = next(((pid, typ) for pid,typ in vs.items() if typ in (0x1b,0x24,0x02)), None)
        if not vp or vp[1] != 0x1b:
            raise RuntimeError('Live TS muxer supports H.264 video only.')
        vpid, vtype = vp
        if audio is None:
            ap = next(((pid,typ) for pid,typ in vs.items() if typ in (0x0f,0x11)), None)
            source_audio = video
        else:
            aus = parse_program(audio)
            ap = next(((pid,typ) for pid,typ in aus.items() if typ in (0x0f,0x11)), None)
            source_audio = audio
        if not ap or ap[1] not in (0x0f,0x11):
            raise RuntimeError('Live TS muxer needs an AAC-LC audio track.')
        apid, atype = ap
        pat, pmt = tables(vtype, atype)
        groups = []
        for pts, packets in pes_groups(video, vpid):
            groups.append((pts, 0x100, packets))
        for pts, packets in pes_groups(source_audio, apid):
            groups.append((pts, 0x101, packets))
        if not groups:
            raise RuntimeError('No elementary MPEG-TS packets found.')
        # Keep relative order within each elementary stream. PTS is the merge
        # clock; discontinuities at segment boundaries are handled by the next PSI.
        last = 0
        for idx, (pts, target, packets) in enumerate(groups):
            if pts is None:
                pts = last
            else:
                last = pts
            groups[idx] = (pts, target, packets)
        groups.sort(key=lambda item: item[0])
        result = bytearray(pat + pmt)
        for _, target, packets in groups:
            for packet in packets:
                out = bytearray(packet)
                out[1] = (out[1] & 0xe0) | ((target >> 8) & 0x1f)
                out[2] = target & 255
                if (out[3] >> 4) & 1:
                    out[3] = (out[3] & 0xf0) | self.counters[target]
                    self.counters[target] = (self.counters[target] + 1) & 15
                result.extend(out)
        return _rebase_ts(self, bytes(result), discontinuity)



def publish_paced(server, data, duration, stop):
    """Transmit TS continuously instead of sending one segment in a burst."""
    chunk = 188 * 70
    duration = max(0.01, float(duration))
    size = len(data)
    start = time.monotonic()

    for pos in range(0, size, chunk):
        if stop.is_set():
            return

        deadline = start + duration * pos / size
        remaining = deadline - time.monotonic()

        if remaining > 0 and stop.wait(remaining):
            return

        server.publish(data[pos:pos+chunk])

    remaining = start + duration - time.monotonic()
    if remaining > 0:
        stop.wait(remaining)


class Broadcaster:
    """Broadcast each TS block independently; slow clients never block others."""

    def __init__(self, family, port=1491):
        self.clients = {}
        self.lock = threading.Lock()
        self.listeners = []
        self.stopped = threading.Event()
        families = [4, 6] if family is None else [int(family)]
        try:
            for version in families:
                sock = socket.socket(socket.AF_INET6 if version == 6 else socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if version == 6:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind(('::1' if version == 6 else '127.0.0.1', port))
                    sock.listen(16)
                    sock.settimeout(0.5)
                    self.listeners.append(sock)
                except Exception:
                    sock.close()
                    raise
                threading.Thread(target=self._accept, args=(sock,), daemon=True).start()
                print('Listening on tcp://' + ('[::1]' if version == 6 else '127.0.0.1') + ':' + str(port))
        except Exception:
            self.close()
            raise

    def _accept(self, listener):
        while not self.stopped.is_set():
            try:
                client, addr = listener.accept()
                client.settimeout(2.0)
                outgoing = queue.Queue(maxsize=64)
                with self.lock:
                    self.clients[client] = outgoing
                threading.Thread(target=self._sender, args=(client, outgoing), daemon=True).start()
                print('TCP client connected:', addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def _sender(self, client, outgoing):
        try:
            while not self.stopped.is_set():
                try:
                    block = outgoing.get(timeout=0.5)
                except queue.Empty:
                    try:
                        ready, _, _ = select.select([client], [], [], 0)
                        if ready:
                            if not client.recv(1, socket.MSG_PEEK):
                                break
                            break
                    except (OSError, ValueError):
                        break
                    continue
                ready, _, _ = select.select([client], [], [], 0)
                if ready:
                    if not client.recv(1, socket.MSG_PEEK):
                        break
                    break
                client.sendall(block)
        except OSError:
            pass
        finally:
            with self.lock:
                self.clients.pop(client, None)
            try:
                client.close()
            except OSError:
                pass

    def publish(self, data):
        with self.lock:
            clients = list(self.clients.items())
        for client, outgoing in clients:
            try:
                outgoing.put_nowait(data)
            except queue.Full:
                # A slow client gets disconnected; never stall live output.
                with self.lock:
                    self.clients.pop(client, None)
                try:
                    client.close()
                except OSError:
                    pass

    def close(self):
        self.stopped.set()
        for listener in self.listeners:
            listener.close()
        with self.lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass




class FFmpegTS:
    """HTTPX media segments -> FFmpeg -> MPEG-TS stdout -> TCP."""

    def __init__(self, p, separate_audio, server=None, output=None):
        import os
        import queue
        import subprocess
        import threading

        self.p = p
        self.server = server
        self.output = output
        self.errors = []
        self.bytes_out = 0
        self.closed = False
        self.queues = []
        self.threads = []

        self.read_fds = []
        self.write_fds = []

        count = 2 if separate_audio else 1

        for _ in range(count):
            r, w = os.pipe()
            self.read_fds.append(r)
            self.write_fds.append(w)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-nostdin",
            "-fflags", "+genpts+discardcorrupt",
            "-dts_delta_threshold", "1",
        ]

        for fd in self.read_fds:
            command += [
                "-thread_queue_size", "512",
                "-f", "mpegts",
                "-i", f"pipe:{fd}"
            ]

        maps = [
            "-map", "0:v:0",
            "-map", "1:a:0" if separate_audio else "0:a:0?"
        ]

        command += maps + [
            "-c", "copy",
            "-max_interleave_delta", "2000000",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-mpegts_flags", "+resend_headers",
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

        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=None,
                pass_fds=tuple(self.read_fds),
                bufsize=0
            )
        except BaseException:
            for fd in self.read_fds + self.write_fds:
                os.close(fd)
            raise

        for fd in self.read_fds:
            os.close(fd)

        with p.RUNNING_LOCK:
            p.RUNNING.add(self.process)

        for fd in self.write_fds:
            q = queue.Queue(maxsize=12)
            self.queues.append(q)

            thread = threading.Thread(
                target=self._feed,
                args=(fd, q),
                daemon=True
            )
            thread.start()
            self.threads.append(thread)

        self.reader = threading.Thread(
            target=self._read_output,
            daemon=True
        )
        self.reader.start()

        print(
            "FFmpeg MPEG-TS remuxer started "
            f"({count} input pipe(s))",
            flush=True
        )

    def _feed(self, fd, q):
        import os

        try:
            with os.fdopen(fd, "wb", buffering=0) as pipe:
                while not self.p.STOP.is_set():
                    block = q.get()

                    if block is None:
                        break

                    view = memoryview(block)

                    while view:
                        written = pipe.write(view)
                        if written is None or written <= 0:
                            raise BrokenPipeError(
                                "FFmpeg input pipe closed"
                            )
                        view = view[written:]

        except (OSError, ValueError) as exc:
            if not self.p.STOP.is_set():
                self.errors.append(exc)

    def _read_output(self):
        try:
            while True:
                block = self.process.stdout.read(188 * 256)
                if not block:
                    break

                self.bytes_out += len(block)

                if self.server is not None:
                    self.server.publish(block)

        except (OSError, ValueError) as exc:
            if not self.p.STOP.is_set():
                self.errors.append(exc)

    def check(self):
        if self.errors:
            raise RuntimeError(
                f"FFmpeg pipeline failed: {self.errors[0]}"
            )

        code = self.process.poll()
        if code is not None and not self.closed:
            raise RuntimeError(
                f"FFmpeg exited unexpectedly: {code}"
            )

    def submit(self, video, audio=None):
        import queue

        blocks = [video]
        if len(self.queues) == 2:
            if audio is None:
                raise RuntimeError(
                    "Separate audio segment missing"
                )
            blocks.append(audio)

        for q, block in zip(self.queues, blocks):
            while not self.p.STOP.is_set():
                self.check()
                try:
                    q.put(block, timeout=0.5)
                    break
                except queue.Full:
                    continue

    def close(self):
        import queue
        import subprocess

        self.closed = True

        if self.p.STOP.is_set():
            self.process.terminate()
        else:
            for q in self.queues:
                while self.process.poll() is None:
                    try:
                        q.put(None, timeout=0.5)
                        break
                    except queue.Full:
                        continue

            for thread in self.threads:
                thread.join(timeout=15)

        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

        self.reader.join(timeout=5)
        self.process.stdout.close()

        with self.p.RUNNING_LOCK:
            self.p.RUNNING.discard(self.process)

        if not self.p.STOP.is_set() and self.process.returncode:
            raise RuntimeError(
                f"FFmpeg failed with exit code "
                f"{self.process.returncode}"
            )


def live_loop(p, channel_id, jwt, family, port=1491):
    import PlutoFetchExtra as extra

    url = urljoin(
        p.PLAYOUT_BASE,
        f"v2/stitch/hls/channel/{channel_id}/master.m3u8"
    )

    video_url, audio_url, quality = p.select_variant(
        p.load_playlist(url, jwt), url
    )

    print("Selected quality:", quality)

    server = Broadcaster(family, port)
    remuxer = None

    try:
        remuxer = FFmpegTS(
            p,
            separate_audio=audio_url is not None,
            server=server
        )

        for item, video, audio in extra._segments(
            p, video_url, audio_url, jwt, True
        ):
            if p.STOP.is_set():
                break

            remuxer.submit(video, audio)

            with server.lock:
                count = len(server.clients)

            print(
                f"Live: segment {item['seq']} | "
                f"HTTPX {len(video)/1048576:.2f} MB | "
                f"clients {count} | "
                f"FFmpeg TS {remuxer.bytes_out/1048576:.2f} MB",
                flush=True
            )
    finally:
        try:
            if remuxer is not None:
                remuxer.close()
        finally:
            server.close()
