# PlutoFetch

PlutoFetch is an unofficial Python command-line tool for downloading Pluto TV VOD episodes, streaming Live TV channels over TCP, recording live broadcasts to MP4, and streaming VOD episodes in real time.

It uses Python HTTPX with HTTP/1.1 and HTTP/2 support for network requests and HLS downloads, supports interactive catalog browsing, and includes a built-in MPEG-TS multiplexer and fragmented MP4 writer.

**Live TV streaming and recording do not require FFmpeg.** FFmpeg is used only for conventional VOD downloading and remuxing.

## Features

### Video on Demand (VOD)

- Download episodes directly from Pluto TV URLs
- Open series URLs and select seasons and episodes interactively
- Search for series by name using `-s`
- Display numbered search results
- Automatically select the highest available video quality
- Select Portuguese audio when available
- Concurrent HLS segment downloads over HTTP/1.1 or HTTP/2
- AES-128 HLS support
- Automatic episode filenames
- Custom output directories and filenames
- SHA-256-based temporary resource filenames
- Download progress with speed, percentage and ETA
- Automatic detection of certain trailing Pluto TV fillers
- Optional inclusion of trailing fillers
- Local FFmpeg remuxing without re-encoding
- Configurable temporary-file cleanup

### Live TV

- Stream Pluto TV channels over TCP
- Search for channels using `-sc`
- Automatically recognize Live TV URLs
- Continuously refresh live HLS playlists
- Download multiple HLS segments concurrently over HTTPX
- Download video and audio segments in parallel
- Preserve HLS segment ordering
- Pre-buffer media before streaming
- FFmpeg-based H.264/AAC MPEG-TS remuxing
- Support multiple simultaneous TCP clients
- Listen on IPv4, IPv6 or both
- Detect disconnected TCP clients
- Record live broadcasts to fragmented MP4
- Record and broadcast simultaneously
- FFmpeg required for live streaming and recording

### Live VOD

- Stream VOD episodes over TCP as MPEG-TS
- Simulate live playback of prerecorded episodes
- Download HLS segments while streaming
- Pre-buffer segments before playback
- Stream without saving a file
- Stream and download an MP4 simultaneously
- Support multiple TCP clients
- Use the same TCP endpoints as Live TV

## HTTP Networking

PlutoFetch uses **Python HTTPX instead of the curl executable** for all HTTP requests and HLS media downloads.

### Features

- HTTP/1.1 support
- HTTP/2 support
- Automatic protocol negotiation using TLS ALPN
- Automatic fallback to HTTP/1.1 when HTTP/2 is unavailable
- TLS with Python's OpenSSL-backed `ssl` module
- TLS certificate verification
- Persistent HTTP connections and connection pooling
- Concurrent HTTP requests and HLS segment downloads
- HTTP response streaming
- In-memory cookies
- Authenticated Pluto TV API requests
- Retry handling for transient HTTP errors and network failures

### Python Dependency

    httpx[http2]

The `httpx[http2]` extra installs the additional dependencies required for HTTP/2.

HTTPX selects HTTP/2 when supported by the remote server and HTTP/1.1 otherwise.

The internal `curl()` compatibility function in `PlutoFetch.py` calls HTTPX and **does not execute the curl binary**.

### HTTP Timeouts and Retries

The current HTTP client configuration includes:

| Setting | Value |
|---------|-------|
| Connect timeout | 12 seconds |
| Read timeout | 25 seconds |
| Write timeout | 25 seconds |
| Connection pool timeout | 15 seconds |
| Maximum connections | 48 |
| Maximum keep-alive connections | 24 |
| Keep-alive expiry | 30 seconds |
| HTTP request attempts | 4 |

Retryable HTTP status codes include `408`, `425`, `429`, `500`, `502`, `503`, and `504`.

Retries do not guarantee recovery from permanently unavailable HLS segments or expired playlist URLs.

### TLS and OpenSSL

HTTPS connections use Python's `ssl` module backed by OpenSSL.

The OpenSSL command-line utility may still be required for AES-128 HLS decryption. It is separate from the HTTP networking implementation.

## Requirements

- Python 3
- HTTPX with HTTP/2 support: `httpx[http2]`
- FFmpeg for all video streaming, recording, and remuxing modes
- OpenSSL executable for conventional VOD AES-128 decryption when needed

Install HTTPX:

```bash
python3 -m pip install 'httpx[http2]'
```

The external `curl` executable is no longer required.
HTTPS uses Python's OpenSSL-backed `ssl` module.

## Files

| File | Purpose |
|------|---------|
| `PlutoFetch.py` | CLI, authentication, HTTPX and VOD downloads |
| `PlutoFetchLive.py` | Channel catalog and TCP broadcaster |
| `PlutoFetchExtra.py` | Live recording and Live VOD entry points |
| `PlutoFetchHLS.py` | Local HLS relay and FFmpeg supervisor |

All four Python files must be in the same directory.

## Usage

### Download a VOD Episode

    python PlutoFetch.py -U "https://pluto.tv/br/shows/1550017/episode/69a9d62add687227e1af99b1/"

### Browse a Series by URL

    python PlutoFetch.py -U "https://pluto.tv/br/shows/1550017/"

The series is resolved from its URL without requiring a title search.

PlutoFetch then displays the available seasons and episodes.

### Search for a Series

    python PlutoFetch.py -s "Bob Esponja"

Matching series are displayed as a numbered list.

After choosing a series, select a season and episode.

### Search for a Channel

    python PlutoFetch.py -sc "Nick"

Matching live channels are displayed as a numbered list.

Selecting a channel starts the TCP streaming server.

### Stream a Live Channel by URL

    python PlutoFetch.py -U "https://pluto.tv/br/live-tv/62545c0b002f4b0007688b61"

### Record Live TV to MP4

    python PlutoFetch.py -sc "Nick" --record-live

The broadcast is transmitted over TCP and simultaneously saved as fragmented MP4.

FFmpeg is not used.

Press Ctrl+C to stop the recording.

### Record Live TV Without TCP

Use `--record-live --record-only` to save the channel directly to MP4 without starting any TCP listener.

    python PlutoFetch.py -sc "Nick" --record-live --record-only

With a custom directory and filename:

    python PlutoFetch.py -sc "Nick" --record-live --record-only -o "/sdcard/Movies" -n "Nickelodeon"

This mode downloads the live HLS segments and writes a fragmented MP4 using Python. FFmpeg is used without opening a TCP listener.

Press Ctrl+C to stop recording.

`--record-only` requires `--record-live`.

### Record Live TV with a Custom Directory and Filename

    python PlutoFetch.py -sc "Nick" --record-live -o "/sdcard/Movies" -n "Nickelodeon"

Output:

    /sdcard/Movies/Nickelodeon.mp4

Without `-o`, recordings are saved in `$HOME`.

Without `-n`, a filename is generated automatically using the channel ID and current date/time.

## Live VOD

Live VOD transmits a prerecorded episode as an MPEG-TS stream over TCP.

Media is downloaded progressively and delivered at a controlled playback rate rather than transmitting the entire episode immediately.

### Mode 1: Stream and Download

    python PlutoFetch.py -U "https://pluto.tv/br/shows/1550017/episode/69a9d62add687227e1af99b1/" --live-vod both

This mode:

- Starts a TCP MPEG-TS server
- Streams the episode in real time
- Downloads and saves a conventional MP4 simultaneously
- Uses FFmpeg for the conventional MP4 download
- Uses FFmpeg to generate MPEG-TS for TCP streaming

The streaming and conventional downloading paths currently perform separate HLS retrieval operations.

### Mode 2: Stream Only

    python PlutoFetch.py -U "https://pluto.tv/br/shows/1550017/episode/69a9d62add687227e1af99b1/" --live-vod only

This mode:

- Streams the episode over TCP
- Does not save an MP4
- Does not require FFmpeg
- Downloads media progressively while streaming

Live VOD waits for a TCP client before starting the media transmission.

## TCP Streaming

By default, both IPv4 and IPv6 listeners are enabled:

    tcp://127.0.0.1:1491
    tcp://[::1]:1491

### Custom TCP Port

Use `-p` or `--port` to select the TCP listening port.

The default is `1491`. Valid values range from `1` to `65535`.

    python PlutoFetch.py -sc "Nick" -p 16000

The listeners become:

    tcp://127.0.0.1:16000
    tcp://[::1]:16000

Custom ports work with Live TV, Live recording with TCP, and Live VOD.

### IPv4 Only

    python PlutoFetch.py -sc "Nick" -l 4

### IPv6 Only

    python PlutoFetch.py -sc "Nick" -l 6

### Both IPv4 and IPv6

Omit the `-l` argument:

    python PlutoFetch.py -sc "Nick"

The `-l` option is supported by Live TV and Live VOD, but not conventional VOD downloads.

### Playback with mpv

IPv6:

    mpv "tcp://[::1]:1491"

IPv4:

    mpv "tcp://127.0.0.1:1491"

Multiple clients can connect to the TCP server simultaneously.

Each client has an independent outgoing queue. Slow clients can be disconnected without blocking the other clients.


## Live HLS Architecture

Live TV and Live VOD use the following pipeline:

```text
Pluto TV HLS
     |
     v
Python HTTPX
     |
     v
Local HLS relay (IPv6 ::1)
     |
     v
FFmpeg HLS demuxer
     |
     v
MPEG-TS stdout (pipe:1)
     |
     v
PlutoFetch TCP broadcaster
     |
     v
Local players
```

The relay serves rewritten playlists, HLS segments and encryption keys
to FFmpeg. HTTPX handles all upstream Pluto TV requests.

FFmpeg reads actual HLS playlists instead of separate raw MPEG-TS
input pipes. Metadata such as `#EXT-X-MEDIA-SEQUENCE`,
`#EXT-X-DISCONTINUITY`, `#EXT-X-KEY`, `#EXTINF` and
`#EXT-X-PROGRAM-DATE-TIME` is preserved.

### IPv6-first relay

The relay binds to IPv6 loopback first:

```text
http://[::1]:<automatic-port>/
```

IPv4 loopback is used only if binding to `::1` fails.

The relay's HTTP port is selected automatically and is independent
of the configurable TCP playback port.

### Live edge delay

Live TV targets a starting position 15 seconds behind the HLS
live edge using:

```text
#EXT-X-START:TIME-OFFSET=-15.0,PRECISE=NO
```

FFmpeg uses `-prefer_x_start 1`. The delay is a target rather
than a continuously enforced latency.

Live VOD starts at the beginning of the prerecorded content.

### FFmpeg logging

FFmpeg logs are displayed in the terminal, including errors involving
timestamps, packets and HLS downloads.

Periodic status reports show connected TCP clients, output bytes,
media progress, time without output and the current FFmpeg attempt.

### Automatic recovery

The Live supervisor monitors FFmpeg output and media progress.

It can restart the FFmpeg process, recreate the HLS relay and
refresh the upstream HLS URLs after detected stalls or failures.

Existing TCP clients are disconnected during recovery so they
can reconnect with a new demuxer. The TCP listener remains active.

Recovery cannot guarantee uninterrupted playback and depends on
the player's reconnection behavior.

### MPEG-TS output

FFmpeg remuxes the HLS media to MPEG-TS using stream copy where
supported. PlutoFetch forwards the FFmpeg `pipe:1` output to
connected TCP clients.

The legacy Python `TsMuxer` implementation may still exist
in the source but is not used by the current Live entry points.

## Live MP4 Recording

Live recording uses FFmpeg to create fragmented MP4 files.

- `--record-live`: save MP4 while broadcasting over TCP.
- `--record-live --record-only`: save MP4 without TCP broadcasting.

Output directory and filename can be customized using `-o` and `-n`.

If recovery restarts FFmpeg during recording, additional MP4 parts
may be created with numbered restart suffixes.

FFmpeg is required for both recording modes.

## VOD Downloading

Conventional VOD mode downloads media resources using Python HTTPX, supporting HTTP/1.1 and HTTP/2.

Video, audio and HLS encryption resources are downloaded to a temporary workspace.

Temporary playlists are rewritten to reference local resources.

FFmpeg then remuxes the downloaded streams into MP4 without re-encoding whenever supported.

### Example Download Progress

    Downloading 708 resources...
    [42.84%] hls_2400-00033.ts | 305/708 | 385.47 MB | 8.57 MB/s | ETA 00:45

### Output Directory

    python PlutoFetch.py -s "Bob Esponja" -o "/sdcard/Movies"

### Custom Filename

    python PlutoFetch.py -s "Bob Esponja" -n "My Episode"

### Concurrent Workers

    python PlutoFetch.py -s "Bob Esponja" -w 10

Conventional VOD downloads use 6 workers by default.

The `-w` option controls conventional VOD downloads, while Live prefetch currently uses its own worker configuration.

## Trailing Filler Detection

Some Pluto TV episodes contain promotional clips or ident animations at the end of their HLS playlists.

PlutoFetch checks certain HLS discontinuities and recognizes trailing segments containing keywords such as:

- ident
- filler
- bumper
- outro
- endcard

Recognized trailing filler clips are excluded from conventional VOD downloads by default.

To include them:

    python PlutoFetch.py -s "Bob Esponja" --include-filler

This detection is heuristic and may not recognize every filler clip.

## Temporary Files

Conventional VOD temporary resources are stored under:

    $HOME/PlutoFetch/

HLS segments are stored under:

    $HOME/PlutoFetch/segments/

Temporary resource filenames use SHA-256 hashes.

Cleanup options:

| Option | Behavior |
|--------|----------|
| Default | Remove the temporary workspace |
| `-k` | Keep temporary files |
| `-ds` | Delete only the segments directory |

## Command-Line Options

| Option | Description |
|--------|-------------|
| `-U`, `--url` | Pluto TV episode, series or live channel URL |
| `-s`, `--search` | Search for a VOD series |
| `-sc`, `--search-channel` | Search for a live channel |
| `-l`, `--listen-on-ip-family` | TCP listener family: 4 or 6 |
| `-p`, `--port` | TCP listening port (default: 1491) |
| `--record-only` | Record Live TV without opening TCP; requires `--record-live` |
| `--record-live` | Record Live TV to fragmented MP4 while broadcasting |
| `--live-vod both` | Stream VOD over TCP and save MP4 |
| `--live-vod only` | Stream VOD over TCP without saving |
| `-o`, `--output` | Output directory |
| `-n`, `--name` | Custom output filename |
| `-w`, `--workers` | Conventional VOD download workers |
| `-k` | Keep all temporary files |
| `-ds` | Delete only temporary segments |
| `--include-filler` | Include recognized trailing fillers |
| `-h`, `--help` | Show help |

## Limitations

Automatic recovery does not guarantee uninterrupted playback. Players may need to reconnect after FFmpeg restarts.

The 15-second live-edge delay is a starting target, not a guaranteed fixed latency.


- Pluto TV may change its APIs without notice.
- Channel and series availability depends on region.
- Some HLS playlist layouts may be unsupported.
- DRM-protected streams are not supported.
- Live HLS remuxing and recovery depend on FFmpeg and source stability.
- Live MP4 recordings may be split into parts after recovery.
- HLS discontinuities may require additional timestamp handling.
- New TCP clients may need to wait for a decodable video keyframe.
- Buffering cannot prevent every network or source-related stall.
- Live streaming currently supports MPEG-TS HLS with H.264 and AAC.
- The TCP server listens on localhost only.
- Live VOD and conventional VOD use different media delivery pipelines.

## Disclaimer

PlutoFetch is an independent, unofficial project.

It is not affiliated with, endorsed by, or sponsored by Pluto TV, Paramount, or their subsidiaries.

Users are responsible for complying with applicable copyright laws, licensing requirements and the streaming service's terms of use.

## License

This project is licensed under the MIT License.

See [LICENSE](LICENSE) for details.


### Independent playback per TCP client

By default, Live VOD uses one shared playback timeline.
All connected TCP clients receive the same current position.

Add `--live-vod-per-client` to give each TCP client its own
playback session starting at the beginning of the episode.

Example:

    python3 PlutoFetch.py -s "Bob Esponja" --live-vod only --live-vod-per-client

With simultaneous MP4 downloading:

    python3 PlutoFetch.py -s "Bob Esponja" --live-vod both --live-vod-per-client

Behavior:

- Every new TCP connection starts at 00:00.
- Each client has its own FFmpeg process and HLS relay.
- Clients can be at different positions in the same episode.
- Disconnecting a client stops only its playback process.
- Finishing an episode does not stop the TCP listener.
- The normal shared Live VOD mode remains available.
- The local HLS relay prefers IPv6 `::1`.

Independent playback consumes additional CPU, memory and
HTTP bandwidth for each connected client.

