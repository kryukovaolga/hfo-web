"""Read a .adicht file in pieces instead of holding it in memory (for the browser).

adichtion maps the whole file (mmap) or, where it cannot, reads it whole and then copies
its sample stream. In WebAssembly (4 GB of memory at most) either way puts the whole
recording in memory: a 24 h file barely fits. Here only the small parts are read at
open time (header, cluster map, the Map stream with the block index, one 20-byte header
per sub-block); sample blocks are read from the file when they are decoded.

This plugs into internals of the pinned adichtion version (1.1.0): AdichtContainer's
data access and AdichtFile._open_adicht / _body. Same decoded samples as AdichtFile
(checked by the tests); recheck when upgrading adichtion.
"""

from __future__ import annotations

import bisect
import dataclasses
import os
import struct
from collections import OrderedDict

from adichtion.blocks import SubBlockIndex
from adichtion.chartdata import parse_chart_data
from adichtion.container import HEADER_SIZE, MAGIC, AdichtContainer, AdichtFormatError, Entry
from adichtion.file import AdichtFile, subblock_at
from adichtion.video import read_videos

PAGE = 1 << 20          # small reads go through a cache of PAGES pages of PAGE bytes
PAGES = 16
SUBBLOCK_HEAD = 20      # 16-byte sub-block header + the 4-byte packed size that follows


class PagedFile:
    """bytes-like read-only view of a file: len() and slices, read on demand."""

    def __init__(self, path: str):
        self.fh = open(path, "rb")
        self.size = os.fstat(self.fh.fileno()).st_size
        self._pages: OrderedDict[int, bytes] = OrderedDict()

    def __len__(self) -> int:
        return self.size

    def _read(self, start: int, n: int) -> bytes:
        self.fh.seek(start)
        return self.fh.read(n)

    def _page(self, k: int) -> bytes:
        page = self._pages.get(k)
        if page is None:
            page = self._pages[k] = self._read(k * PAGE, PAGE)
            if len(self._pages) > PAGES:
                self._pages.popitem(last=False)
        else:
            self._pages.move_to_end(k)
        return page

    def __getitem__(self, s: slice) -> bytes:
        start, stop, step = s.indices(self.size)
        if step != 1:
            raise ValueError("only contiguous slices")
        if stop <= start:
            return b""
        if stop - start > PAGE:
            return self._read(start, stop - start)
        k0, k1 = start // PAGE, (stop - 1) // PAGE
        if k0 == k1:
            return self._page(k0)[start - k0 * PAGE:stop - k0 * PAGE]
        return self._page(k0)[start - k0 * PAGE:] + self._page(k1)[:stop - k1 * PAGE]

    def close(self) -> None:
        self.fh.close()


class LazyContainer(AdichtContainer):
    """AdichtContainer whose `data` is a PagedFile (slices only, no buffer protocol)."""

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        self._views = []
        self.data = PagedFile(self.path)
        self._fh = self.data.fh
        head = self.data[0:HEADER_SIZE]
        if len(head) < HEADER_SIZE or head[:len(MAGIC)] != MAGIC:
            self.close()
            raise AdichtFormatError(f"{self.path!r} is not an ADInstruments Compound File")
        (self.version, self.header_size, self.chunk_size, _, _, _, log2cl) = struct.unpack_from("<7i", head, 0x80)
        if not (8 <= log2cl <= 20) or not (HEADER_SIZE <= self.header_size < len(self.data)):
            self.close()
            raise AdichtFormatError("implausible container header")
        self.cluster_size = 1 << log2cl
        self.capacity, self.neg_capacity, self.map_first = struct.unpack_from("<qqq", head, 0x9C)
        self.root = Entry.from_bytes(head[0xB8:0xB8 + 160])
        self.n_clusters = (len(self.data) - self.header_size) // self.cluster_size
        self.entries_per_page = self.cluster_size // 8
        self._tree = None

    def map_entry(self, cluster: int) -> tuple[int, int]:
        page, idx = divmod(cluster, self.entries_per_page)
        o = self.offset(self.map_first + page) + idx * 8
        if cluster < 0 or o < self.header_size or o + 8 > len(self.data):
            raise AdichtFormatError(f"the cluster map entry of cluster {cluster} lies outside the file")
        return struct.unpack("<ii", self.data[o:o + 8])


class LazyStream:
    """A stream of the container, read on demand: stream offsets -> file offsets."""

    def __init__(self, container: LazyContainer, entry: Entry):
        self.data, self.size = container.data, entry.size
        cs = container.cluster_size
        if container._is_contiguous(entry.first, entry.last):
            runs = [(0, container.offset(entry.first))]
        else:
            runs, pos, prev = [], 0, None
            for c in container.chain(entry.first, entry.last):
                if prev is None or c != prev + 1:
                    runs.append((pos, container.offset(c)))
                pos, prev = pos + cs, c
        self._starts = [r[0] for r in runs]
        self._runs = runs + [(float("inf"), 0)]

    def __len__(self) -> int:
        return self.size

    def read(self, start: int, stop: int) -> bytes:
        stop = min(stop, self.size)
        out, pos = [], start
        i = bisect.bisect_right(self._starts, pos) - 1
        while pos < stop:
            run_start, file_off = self._runs[i]
            end = min(stop, self._runs[i + 1][0])
            out.append(self.data[file_off + pos - run_start:file_off + end - run_start])
            pos, i = end, i + 1
        return b"".join(out)


class LazyAdichtFile(AdichtFile):
    """AdichtFile that reads its sample blocks from the file when they are decoded."""

    def _open_adicht(self, zero_copy=None) -> None:
        self._container = LazyContainer(self.path)
        try:
            self._map_entry = self._container.find_stream("Map")
            self._blocks_entry = self._container.find_stream("Blocks_0")
            if self._map_entry is None or self._blocks_entry is None:
                raise AdichtFormatError("file has no Map / Blocks_0 stream")
            self.chart = parse_chart_data(self._container.read_stream(self._map_entry))
            if self.chart.parse_mode != "sequential":
                raise AdichtFormatError("reading in pieces needs the Map stream's block index")
            self.zero_copy = False
            self._blocks = LazyStream(self._container, self._blocks_entry)
            subblocks = []
            for rec in self.chart.records:
                for b in range(len(rec.blocks)):
                    for ch in rec.channels:
                        off = ch.blocks[b].offset
                        sb = subblock_at(memoryview(self._blocks.read(off, off + SUBBLOCK_HEAD)), 0,
                                         len(subblocks), f"block index of record {rec.index + 1} "
                                         f"channel {ch.channel + 1} block {b + 1}")
                        if off + 16 + sb.body_size > len(self._blocks):
                            raise AdichtFormatError(f"sub-block at {off} runs past the Blocks stream")
                        subblocks.append(dataclasses.replace(sb, offset=off, body_offset=off + 16))
            dtypes = [c.dtype for c in self.chart.records[0].channels] if self.chart.records else None
            self.index = SubBlockIndex(subblocks, [[b.ticks for b in r.blocks] for r in self.chart.records],
                                       dtypes or None)
            if self.index.n_channels != self.chart.n_channels:
                raise AdichtFormatError(f"{self.index.n_channels} channels of sample data for "
                                        f"{self.chart.n_channels} channel entries")
            self.videos = read_videos(self._container)
        except Exception:
            self._blocks = b""
            self._container.close()
            raise

    def _body(self, sb) -> memoryview:
        return memoryview(self._blocks.read(sb.body_offset, sb.body_offset + sb.body_size))
