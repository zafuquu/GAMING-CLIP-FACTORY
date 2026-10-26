# Environment audit

The hosted coding environment is not the user's Windows PC. The user reported PowerShell 5.1 and FFmpeg/FFprobe 9.0.2 version checks passed in the extracted package, but synthetic-video generation failed during stderr handling; a complete local FFmpeg acceptance test is not confirmed.

Before deployment verify Windows version, Python and Node versions, CPU/RAM/disk, actual FFmpeg and FFprobe paths/build features, codecs, package notices/licenses, data/media paths and permissions, and any future YouTube OAuth setup. Do not treat this note as a verified inventory of the user's PC.
