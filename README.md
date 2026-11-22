# HFO Web

**Open the app: https://kryukovaolga.github.io/hfo-web/**

> **Work in progress.** A research tool under development, not a medical device: results may change between versions, and automatic detections must be reviewed by a person. Use it at your own risk (see the warranty disclaimer in `LICENSE`).

Automatic detection of high frequency oscillations (HFOs) in EEG / iEEG recordings, in the browser. Choose one or more `.edf` files, check the settings, press *Start*: for each recording and each frequency band you get two text files in the format of [HFOApp](https://www.eneuro.org/content/8/5/ENEURO.0509-20.2021), `<recording>_<band>_<low>-<high>Hz_events.txt` (one line per event) and `..._summary.txt` (counts and durations per channel).

- **Nothing to install**: made for current Chrome, Edge, Firefox and Safari, on Windows or macOS.
- **Your recordings never leave your computer.** The page downloads only the program; the analysis runs inside the browser and the result files are saved on your computer. You can check it: once the page has loaded, disconnect from the internet — the analysis still works.
- **Long recordings**: 24 h at 4 kHz take about 6–12 minutes per band, depending on the computer.
- **LabChart files** (`.adicht`, ADInstruments) are accepted too: the page converts them to EDF first, without LabChart, then analyzes them like any EDF (see below).

## What it computes

The default detector of HFOApp (*Hilbert Detector (HFOApp)*, Zhou et al., eNeuro 2021): band-pass filter, Hilbert envelope, moving z-score, onset and inclusion thresholds, minimum number of cycles. It is a Python re-implementation of the MATLAB code, checked against HFOApp's own output (same events, same values). The default settings differ from HFOApp's: two bands (100–250 Hz and 250–800 Hz) with their own thresholds, and an *Epoch* of 600 s instead of the whole recording. Everything can be changed in the page.

Also available: *Hilbert Detector (RippleLab)*.

## LabChart files

A `.adicht` file is converted in the page, then each part is analyzed:

- **one EDF per block** (LabChart starts a new block each time the recording is stopped and restarted): `<name>.edf`, or `<name>_block01.edf`, `_block02.edf`, … — the samples are the stored integer values, unchanged;
- **the comments** in a table, `<name>_events.tsv` (+ `.json` describing the columns), following the [BIDS](https://bids-specification.readthedocs.io) events format: time from the start of the file, local date and time, sample, channel, text;
- `<name>_channels.tsv` (channels, units, sampling rate, hardware filters) and `<name>_recording.json` (exact start time, scale of each channel).

Saving the EDF can be switched off (it is large: about 0.17 GB per hour with 6 channels at 4 kHz). Supported: LabChart for Windows files with recorded channels at one sampling rate; calculated channels, LabChart for Mac documents and files above about 1.85 GB (≈ 24 h with 6 channels at 4 kHz; memory limit of the browser) are refused with a message.

## How it works

The page runs Python ([Pyodide](https://pyodide.org): Python, numpy and scipy compiled to WebAssembly) inside the browser:

- `src/hfo_port/` — the detector (filters, envelope, z-score, events, HFOApp's file format);
- `src/eeg_convert/` — LabChart `.adicht` → EDF + tables, using the open-source reader [adichtion](https://github.com/emilwalsted/adichtion) (pinned version, checked by SHA-256 when the page is built);
- `web/` — the page (`static/`), the browser adapter (`py/hfo_web.py`) and the build script (`build.py`);
- long recordings are processed in blocks; temporary arrays are kept in the browser's private storage on your disk and deleted at the end;
- several channels are analyzed at the same time, depending on processor, memory and disk space.

Run it on your own computer:

```bash
python3 web/build.py --serve      # then open http://localhost:8000
```

## Credits and license

Based on **HFOApp** by Zhou et al.: *HFOApp: A MATLAB graphical user interface for high frequency oscillation marking*, eNeuro 8(5), 2021, https://www.eneuro.org/content/8/5/ENEURO.0509-20.2021. HFOApp is distributed under the GNU GPL v3; this re-implementation is distributed under the same license (`LICENSE`).

LabChart files are read with **adichtion** (https://github.com/emilwalsted/adichtion, GNU GPL v3 or later), included in the page with its license. LabChart and ADInstruments are trademarks of their owner; this tool is not affiliated with ADInstruments.
