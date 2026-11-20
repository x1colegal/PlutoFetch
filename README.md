# PlutoFetch

PlutoFetch is an unofficial Python command-line tool for downloading Pluto TV VOD episodes and streaming live Pluto TV channels over local TCP connections.

It uses curl for HTTP requests and HLS segment downloads, supports interactive catalog browsing, and provides a built-in MPEG-TS multiplexer for live streams.

Live streaming does not require FFmpeg. FFmpeg is used only for VOD remuxing.

## Features

### Video on Demand (VOD)

- Download Pluto TV episodes from their URLs
- Automatically detect VOD episode URLs
- Open series URLs without specifying an episode
- Interactive season and episode selection
- Search for series by name
- Display numbered search results
- Select a series from the results
- Automatically select the highest available video bitrate
- Select Portuguese audio when available
- Concurrent HLS segment downloads using curl
- AES-128 HLS support
- SHA-256-based temporary filenames
- Download progress with speed, percentage, transferred data and ETA
- Automatic episode filenames
- Custom output directories and filenames
- Detect and remove certain trailing Pluto TV fillers
- Optional inclusion of trailing fillers
- Local FFmpeg remuxing without re-encoding
- Configurable temporary-file cleanup
- Ctrl+C interruption handling

### Live TV

- Stream Pluto TV live channels
- Automatically detect live channel URLs
- Search for channels by name
- Display numbered channel search results
- Interactive channel selection
- Download live HLS segments using curl
- Continuously refresh live playlists
- Support AES-128-encrypted HLS segments
- Built-in MPEG-TS multiplexing
- Combine separate H.264 video and AAC audio streams
- Handle MPEG-TS PAT, PMT, PIDs and continuity counters
- Stream MPEG-TS over TCP
- Listen on IPv4, IPv6 or both simultaneously
- Support multiple connected TCP clients
- No FFmpeg required for live streaming
- Stop live streaming with Ctrl+C

## Requirements

- Python 3
- curl
- FFmpeg (VOD only)
- OpenSSL (for AES-128 HLS decryption when needed)

## Usage

### Download a VOD Episode

    python PlutoFetch.py -U "https://pluto.tv/br/shows/1550017/episode/69a9d62add687227e1af99b1/"

PlutoFetch automatically detects the episode and starts the VOD download.

### Browse a Series by URL

    python PlutoFetch.py -U "https://pluto.tv/br/shows/1550017/"

PlutoFetch resolves the series from its URL and opens the season and episode selection menus.

There is no need to search for the series manually.

### Search for a Series

    python PlutoFetch.py -s "Bob Esponja"

Search results are displayed as a numbered list.

Select a series, then choose its season and episode.

### Search for a Live Channel

    python PlutoFetch.py -sc "Nick"

PlutoFetch displays matching channels in a numbered list.

Select a channel to begin live streaming.

### Stream a Live Channel by URL

    python PlutoFetch.py -U "https://pluto.tv/br/live-tv/645951c0e94c38000802d2cb"

The URL is automatically recognized as a live channel.

PlutoFetch opens a local TCP MPEG-TS server.

### Live TCP Endpoints

By default, PlutoFetch listens on both:

    tcp://127.0.0.1:1491
    tcp://[::1]:1491

Multiple clients can connect simultaneously.

All connected clients receive the same live broadcast.

### Listen on IPv4 Only

    python PlutoFetch.py -sc "Nick" -l 4

Endpoint:

    tcp://127.0.0.1:1491

### Listen on IPv6 Only

    python PlutoFetch.py -sc "Nick" -l 6

Endpoint:

    tcp://[::1]:1491

### Listen on Both IPv4 and IPv6

Simply omit the -l argument:

    python PlutoFetch.py -sc "Nick"

The -l / --listen-on-ip-family option is available only for Live TV.

Using this option with VOD produces an error.

## Command-Line Options

| Option | Description |
|--------|-------------|
| `-U` | Pluto TV VOD, series or live channel URL |
| `-s`, `--search` | Search for a series |
| `-sc`, `--search-channel` | Search for a live TV channel |
| `-l`, `--listen-on-ip-family` | Live TCP IP family: 4 or 6 |
| `-o`, `--output` | VOD output directory |
| `-n`, `--name` | Custom VOD filename |
| `-w`, `--workers` | Concurrent download workers |
| `-k` | Keep temporary files |
| `-ds` | Delete only the temporary segments directory |
| `--include-filler` | Include recognized trailing filler clips |
| `-h`, `--help` | Display help |

## Automatic URL Detection

PlutoFetch automatically determines the operation based on the supplied URL.

| URL Type | Operation |
|----------|-----------|
| VOD episode | Download the episode |
| Series page | Open season and episode selection |
| Live TV channel | Start the TCP live stream |

Examples:

VOD episode:

    https://pluto.tv/br/shows/1550017/episode/69a9d62add687227e1af99b1/

Series:

    https://pluto.tv/br/shows/1550017/

Live channel:

    https://pluto.tv/br/live-tv/645951c0e94c38000802d2cb

## Interactive Search

### Series Search

Use -s to search the Pluto TV series catalog.

Matching series are displayed in a numbered list.

After choosing a series, PlutoFetch displays its available seasons.

After selecting a season, the available episodes are displayed.

The selected episode is then downloaded using the VOD downloader.

### Channel Search

Use -sc to search the Pluto TV channel catalog.

Matching channels are displayed in a numbered list.

After selecting a channel, PlutoFetch starts the live streaming server.

Search results and channel availability depend on the Pluto TV catalog and region.

## Live Streaming Architecture

PlutoFetch retrieves live HLS playlists and downloads new media segments using curl.

When video and audio are delivered separately, the built-in MPEG-TS multiplexer combines them into a single transport stream.

Architecture:

    Pluto TV Live HLS
           |
          curl
           |
       +---+---+
       |       |
     Video    Audio
       |       |
       +---+---+
           |
    Python MPEG-TS Muxer
           |
     MPEG-TS Stream
           |
      TCP Broadcast
           |
      +----+----+
      |         |
    IPv4       IPv6
      |         |
    Clients   Clients

The TCP server supports multiple simultaneous connections.

FFmpeg is not used for live streaming.

### MPEG-TS Multiplexing

The built-in multiplexer processes MPEG-TS packets and combines separate H.264 and AAC streams.

It handles:

- Program Association Tables (PAT)
- Program Map Tables (PMT)
- Packet identifiers (PIDs)
- Continuity counters
- Presentation timestamps (PTS)
- Decoding timestamps (DTS), when available

Live multiplexing is experimental. Certain stream discontinuities, timestamp changes or unusual HLS layouts may require additional handling.

## VOD Downloading

VOD downloading uses curl to retrieve HLS resources concurrently.

Video and audio resources are downloaded separately when necessary.

Temporary playlists are rewritten to reference local files.

FFmpeg then remuxes the downloaded streams into an MP4 file without re-encoding whenever possible.

### Download Progress

Example:

    Downloading 708 resources...
    [42.84%] hls_2400-00033.ts | 305/708 | 385.47 MB | 8.57 MB/s | ETA 00:45

Progress information includes:

- Original HLS segment filename
- Completed resource count
- Total resource count
- Completion percentage
- Downloaded data
- Average download speed
- Estimated remaining time

## VOD Output Options

### Custom Output Directory

    python PlutoFetch.py -s "Bob Esponja" -o "/sdcard/Download"

### Custom Filename

    python PlutoFetch.py -s "Bob Esponja" -n "My Episode"

### Concurrent Downloads

    python PlutoFetch.py -s "Bob Esponja" -w 10

The default number of concurrent workers is 6.

## Trailing Filler Detection

Some Pluto TV episodes contain trailing promotional clips or ident animations.

PlutoFetch can recognize certain trailing segments by inspecting HLS discontinuities and segment URLs.

Keywords include:

- ident
- filler
- bumper
- outro
- endcard

Recognized trailing filler clips are excluded by default.

To include them:

    python PlutoFetch.py -s "Bob Esponja" --include-filler

Detection is heuristic and may not identify every filler clip.

## Temporary Files

Temporary files are stored in:

    $HOME/PlutoFetch/

Downloaded segments are stored in:

    $HOME/PlutoFetch/segments/

Temporary resource filenames are generated using SHA-256 hashes.

This helps prevent filename collisions.

Available cleanup modes:

| Mode | Behavior |
|------|----------|
| Default | Remove the temporary workspace |
| `-k` | Keep the temporary workspace |
| `-ds` | Remove only the segments directory |

The selected cleanup policy also applies to interrupted VOD downloads.

## Example VOD Output

An automatically generated filename may look like:

    Bob Esponja Calça Quadrada - S08E20 - Careta definitiva! - Mundo da Luva, descanse em paz.mp4

The resulting MP4 preserves the original video and audio codecs when stream copying is supported.

## Example Live Output

After selecting a channel:

    Live channel: 645951c0e94c38000802d2cb
    Selected quality: Highest bitrate (3321280 bps)
    Listening on tcp://127.0.0.1:1491
    Listening on tcp://[::1]:1491

    Live: segment 18 | 1.61 MB | clients 1
    Live: segment 19 | 1.60 MB | clients 1
    Live: segment 20 | 1.52 MB | clients 1

The clients counter indicates the number of currently connected TCP clients.

## Limitations

- Pluto TV APIs may change without notice.
- Catalog availability depends on geographic region.
- Some HLS playlist structures may be unsupported.
- DRM-protected content is not supported.
- Live streaming depends on the availability of the source channel.
- MPEG-TS multiplexing is experimental.
- Playback quality and compatibility depend on the client.
- New live clients may need to wait for a decodable video keyframe.
- Trailing filler detection is not guaranteed to recognize every clip.

## Disclaimer

PlutoFetch is an independent, unofficial project.

It is not affiliated with, endorsed by, or sponsored by Pluto TV, Paramount or their subsidiaries.

Users are responsible for complying with applicable copyright laws, licensing requirements and the streaming service's terms of use.

## License

This project is licensed under the MIT License.

See [LICENSE](LICENSE) for details.
