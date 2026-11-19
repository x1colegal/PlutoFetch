# PlutoFetch

PlutoFetch is a Python command-line downloader for Pluto TV HLS streams.

It uses curl to download media resources and FFmpeg to merge locally downloaded video and audio into MP4 files.

No browser, Node.js, or JavaScript runtime is required.

## Features

- Automatic Pluto TV session initialization
- Cookies and session tokens stored in memory
- HLS media downloads using curl
- Concurrent segment downloads
- Automatic selection of the highest available video quality
- Portuguese audio selection when available
- Original segment filenames displayed during downloads
- SHA-256-based temporary filenames
- Download progress with transferred size, speed, and ETA
- Automatic episode filenames
- Custom output directories and filenames
- Automatic detection of certain trailing Pluto TV bumpers
- Optional inclusion of trailing bumpers
- Local FFmpeg merging without network downloads
- Configurable temporary-file cleanup
- Ctrl+C interruption handling

## Requirements

- Python 3
- curl
- FFmpeg

No third-party Python packages are required.

## Usage

Download an episode:

    python PlutoFetch.py -U "EPISODE_URL"

Choose an output directory:

    python PlutoFetch.py -U "EPISODE_URL" -o "/sdcard/Download"

Set a custom filename:

    python PlutoFetch.py -U "EPISODE_URL" -n "Episode Name"

Keep all temporary files:

    python PlutoFetch.py -U "EPISODE_URL" -k

Delete only the segments directory:

    python PlutoFetch.py -U "EPISODE_URL" -ds

Include trailing Pluto TV bumpers:

    python PlutoFetch.py -U "EPISODE_URL" --include-filler

Use 10 concurrent downloads:

    python PlutoFetch.py -U "EPISODE_URL" -w 10

## Command-Line Options

| Option | Description |
|--------|-------------|
| -U, --url | Pluto TV episode URL |
| -o, --output | Output directory |
| -n, --name | Custom output filename |
| -w, --workers | Number of concurrent downloads |
| -k | Keep all temporary files |
| -ds | Delete segments but keep the workspace |
| --include-filler | Include trailing Pluto TV bumpers |
| -h, --help | Display help |

## How It Works

1. Retrieves episode metadata.
2. Initializes a Pluto TV session.
3. Requests an HLS streaming playlist.
4. Selects the highest available video quality.
5. Selects an appropriate audio track.
6. Checks for recognized trailing Pluto TV bumpers.
7. Downloads HLS segments and encryption-key resources using curl.
8. Rewrites playlists to reference local files.
9. Merges downloaded video and audio using FFmpeg.
10. Removes temporary files according to the selected cleanup policy.

FFmpeg operates on local files only. All network downloads are handled by curl.

## Download Progress

PlutoFetch displays:

- Original segment filename
- Completed and total resources
- Percentage completed
- Downloaded data
- Average download speed
- Estimated remaining time

Example:

    Downloading 708 resources...
    [ 42.84%] hls_2400-00033.ts | 305/708 | 385.47 MB | 8.57 MB/s | ETA 00:45

Download speeds are displayed automatically using B/s, KB/s, MB/s, or GB/s.

The ETA is estimated using the average resource size and download speed.

## Trailing Bumper Detection

Pluto TV may append promotional clips or ident animations to the end of an episode.

PlutoFetch can detect some of these clips by inspecting HLS discontinuities and matching keywords in segment URLs.

Recognized keywords include:

- ident
- filler
- bumper
- outro
- endcard

When a recognized trailing bumper is detected, its segments are excluded from the download.

Use --include-filler to disable this filtering.

This detection is heuristic and may not identify every promotional clip.

## Temporary Files

Temporary resources are stored in:

    $HOME/PlutoFetch/segments/

Downloaded resources use SHA-256-based filenames to prevent filename collisions.

The original segment filenames are displayed in the terminal.

By default, the temporary workspace is removed after successful completion.

Cleanup modes:

- Default: delete the entire PlutoFetch workspace.
- -k: keep all temporary files.
- -ds: delete only the segments directory.

The selected cleanup policy also applies when interrupted with Ctrl+C.

## Output

The final video is saved as an MP4 file.

By default, output files are saved in the user's home directory.

Use -o to select another directory.

Automatic filenames include:

- Series title
- Season number
- Episode number
- Episode title

Example:

    Bob Esponja Calça Quadrada - S08E20 - Careta definitiva! - Mundo da Luva, descanse em paz.mp4

Video and audio streams are copied without re-encoding whenever supported.

## Limitations

- Pluto TV may change its APIs or streaming infrastructure.
- Some HLS playlist structures may not be supported.
- Certain encrypted or DRM-protected streams may not be supported.
- Automatic session initialization depends on Pluto TV accepting the request.
- Trailing bumper detection is not guaranteed to recognize every promotional clip.
- Content availability depends on region and catalog availability.

## Disclaimer

PlutoFetch is an independent, unofficial project.

It is not affiliated with, endorsed by, or sponsored by Pluto TV, Paramount, or their subsidiaries.

Users are responsible for complying with applicable copyright laws and the streaming service's terms of use.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
