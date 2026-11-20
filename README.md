# PlutoFetch

PlutoFetch is an unofficial Python command-line tool for downloading Pluto TV VOD episodes, streaming Live TV channels over TCP, recording live broadcasts to MP4, and streaming VOD episodes in real time.

It uses curl for HTTP requests and HLS downloads, supports interactive catalog browsing, and includes a built-in MPEG-TS multiplexer and fragmented MP4 writer.

**Live TV streaming and recording do not require FFmpeg.** FFmpeg is used only for conventional VOD downloading and remuxing.

## Features

### Video on Demand (VOD)

- Download episodes directly from Pluto TV URLs
- Open series URLs and select seasons and episodes interactively
- Search for series by name using `-s`
- Display numbered search results
- Automatically select the highest available video quality
- Select Portuguese audio when available
- Concurrent HLS segment downloads
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
- Download multiple HLS segments concurrently
- Download video and audio segments in parallel
- Preserve HLS segment ordering
- Pre-buffer media before streaming
- Built-in H.264/AAC MPEG-TS multiplexing
- Support multiple simultaneous TCP clients
- Listen on IPv4, IPv6 or both
- Detect disconnected TCP clients
- Record live broadcasts to fragmented MP4
- Record and broadcast simultaneously
- No FFmpeg required for live streaming or recording

### Live VOD

- Stream VOD episodes over TCP as MPEG-TS
- Simulate live playback of prerecorded episodes
- Download HLS segments while streaming
- Pre-buffer segments before playback
- Stream without saving a file
- Stream and download an MP4 simultaneously
- Support multiple TCP clients
- Use the same TCP endpoints as Live TV

## Requirements

- Python 3
- curl
- FFmpeg (conventional VOD downloading only)
- OpenSSL (for AES-128 HLS decryption when needed)

No third-party Python packages are required.

## Files

| File | Purpose |
|------|---------|
| `PlutoFetch.py` | Main CLI, authentication and VOD downloader |
| `PlutoFetchLive.py` | Live HLS, catalog search, MPEG-TS muxer and TCP server |
| `PlutoFetchExtra.py` | Live MP4 recording, parallel prefetch and Live VOD |

All three Python files must be located in the same directory.

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
- Uses the Python MPEG-TS multiplexer for TCP streaming

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

## Live Buffering and Parallel Downloads

PlutoFetch uses a separate prefetch worker to retrieve media ahead of transmission.

The current configuration in `PlutoFetchExtra.py` is:

| Setting | Value |
|---------|-------|
| Maximum concurrent HLS workers | 10 |
| Initial ready-media target | 30 seconds |
| Minimum queued-media reserve | 15 seconds |
| Maximum queued-media target | 60 seconds |
| Live playlist polling interval | 650 ms |

Video and audio resources can be requested concurrently.

Segments are transmitted in increasing HLS media-sequence order. Previously queued or older sequence numbers are skipped to prevent old segments from being appended after newer ones.

### Buffering Behavior

PlutoFetch attempts to accumulate enough fully downloaded media before starting transmission.

For example, with 30 seconds of downloaded content:

    Downloaded:  [==============================] 30s
    Transmitted: [===============               ] 15s
    Reserved:                   [===============] 15s

The exact amount of buffered media depends on HLS segment durations, playlist availability and download completion times.

The buffer settings are targets rather than guaranteed end-to-end playback latency.

The TCP player's own buffer is independent of PlutoFetch's internal queue.

If the source stops publishing segments or a required download is delayed, playback may still stall.

### Segment Ordering

Live playback follows the HLS media sequence.

Example:

    Live: segment 13
    Live: segment 14
    Live: segment 15
    Live: segment 16
    Live: segment 17

Older sequence numbers are not reinserted after newer segments have already been queued.

## MPEG-TS Multiplexing

The built-in MPEG-TS muxer combines H.264 video and AAC audio streams when they are supplied separately.

It handles:

- Program Association Tables (PAT)
- Program Map Tables (PMT)
- Packet identifiers (PIDs)
- Continuity counters
- PTS and DTS information
- MPEG-TS timestamp rebasing

The muxer processes MPEG-TS packets directly in Python.

No FFmpeg process is used for Live TV multiplexing.

### Timestamp Handling

The multiplexing pipeline includes timestamp normalization intended to prevent players from displaying large inherited timestamps from the original live feed.

Timestamp handling is experimental and may require additional work for unusual HLS discontinuities or timestamp resets.

## Live MP4 Recording

PlutoFetch includes a Python fragmented MP4 writer.

The writer processes H.264 and AAC carried in MPEG-TS and generates fragmented MP4 data using ISO BMFF structures.

The output is written incrementally while the Live TV channel continues broadcasting over TCP.

Recording features include:

- Fragmented MP4 output
- H.264 video
- AAC audio
- Automatic filenames
- Custom filenames
- Custom output directories
- Recording and TCP broadcasting at the same time
- No FFmpeg subprocess

The fragmented MP4 writer is experimental.

Some players may handle unfinished or interrupted recordings differently.

## VOD Downloading

Conventional VOD mode downloads media resources using curl.

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

- Pluto TV may change its APIs without notice.
- Channel and series availability depends on region.
- Some HLS playlist layouts may be unsupported.
- DRM-protected streams are not supported.
- Live MPEG-TS multiplexing is experimental.
- Live MP4 recording is experimental.
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
