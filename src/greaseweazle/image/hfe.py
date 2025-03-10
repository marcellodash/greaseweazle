# greaseweazle/image/hfe.py
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

from __future__ import annotations
from typing import Dict, Tuple, Optional

import struct

from greaseweazle import error
from greaseweazle.codec.ibm import ibm
from greaseweazle.track import MasterTrack, RawTrack
from bitarray import bitarray
from .image import Image

class HFEOpts:
    """bitrate: Bitrate of new HFE image file.
    """
    
    def __init__(self) -> None:
        self._bitrate: Optional[int] = None

    @property
    def bitrate(self) -> Optional[int]:
        return self._bitrate
    @bitrate.setter
    def bitrate(self, bitrate: float):
        try:
            self._bitrate = int(bitrate)
            if self._bitrate <= 0:
                raise ValueError
        except ValueError:
            raise error.Fatal("HFE: Invalid bitrate: '%s'" % bitrate)


class HFETrack:
    def __init__(self, bits: bitarray) -> None:
        self.bits = bits

    @classmethod
    def from_hfe_bytes(cls, b: bytes) -> HFETrack:
        bits = bitarray(endian='big')
        bits.frombytes(b)
        bits.bytereverse()
        return cls(bits)

    def to_hfe_bytes(self) -> bytes:
        bits = bitarray(endian='big')
        bits.frombytes(self.bits.tobytes())
        bits.bytereverse()
        return bits.tobytes()


class HFE(Image):

    def __init__(self) -> None:
        self.opts = HFEOpts()
        # Each track is (bitlen, rawbytes).
        # rawbytes is a bytes() object in little-endian bit order.
        self.to_track: Dict[Tuple[int,int], HFETrack] = dict()


    @classmethod
    def from_file(cls, name: str):

        with open(name, "rb") as f:
            dat = f.read()

        (sig, f_rev, n_cyl, n_side, t_enc, bitrate,
         _, _, _, tlut_base) = struct.unpack("<8s4B2H2BH", dat[:20])
        error.check(sig != b"HXCHFEV3", "HFEv3 is not supported")
        error.check(sig == b"HXCPICFE" and f_rev <= 1, "Not a valid HFE file")
        error.check(0 < n_cyl, "HFE: Invalid #cyls")
        error.check(0 < n_side < 3, "HFE: Invalid #sides")

        hfe = cls()
        hfe.opts.bitrate = bitrate

        tlut = dat[tlut_base*512:tlut_base*512+n_cyl*4]
        
        for cyl in range(n_cyl):
            for side in range(n_side):
                offset, length = struct.unpack("<2H", tlut[cyl*4:(cyl+1)*4])
                todo = length // 2
                tdat = bytes()
                while todo:
                    d_off = offset*512 + side*256
                    d_nr = 256 if todo > 256 else todo
                    tdat += dat[d_off:d_off+d_nr]
                    todo -= d_nr
                    offset += 1
                hfe.to_track[cyl,side] = HFETrack.from_hfe_bytes(tdat)

        return hfe


    def get_track(self, cyl: int, side: int) -> Optional[MasterTrack]:
        if (cyl,side) not in self.to_track:
            return None
        assert self.opts.bitrate is not None
        t = self.to_track[cyl,side]
        track = MasterTrack(
            bits = t.bits,
            time_per_rev = len(t.bits) / (2000*self.opts.bitrate))
        return track


    def emit_track(self, cyl: int, side: int, track) -> None:
        # HFE convention is that FM is recorded at double density
        is_fm = (issubclass(type(track), ibm.IBMTrack)
                 and track.mode is ibm.Mode.FM)
        t = track.raw_track() if hasattr(track, 'raw_track') else track
        if self.opts.bitrate is None:
            error.check(hasattr(t, 'bitrate'),
                        'HFE: Requires bitrate to be specified'
                        ' (eg. filename.hfe::bitrate=500)')
            self.opts.bitrate = round(t.bitrate / 2e3)
            if is_fm:
                self.opts.bitrate *= 2
            print('HFE: Data bitrate detected: %d kbit/s' % self.opts.bitrate)
        if issubclass(type(t), MasterTrack):
            # Rotate data to start at the index.
            index = -t.splice % len(t.bits)
            bits = t.bits[index:] + t.bits[:index]
            if is_fm: # FM data is recorded to HFE at double rate
                double_bytes = ibm.encode(bits.tobytes())
                double_bits = bitarray(endian='big')
                double_bits.frombytes(double_bytes)
                bits = double_bits[:2*len(bits)]
        else:
            flux = t.flux()
            flux.cue_at_index()
            raw = RawTrack(clock = 5e-4 / self.opts.bitrate, data = flux)
            bits, _ = raw.get_revolution(0)
        self.to_track[cyl,side] = HFETrack(bits)


    def get_image(self) -> bytes:

        n_side = 1
        n_cyl = max(self.to_track.keys(), default=(0,), key=lambda x:x[0])[0]
        n_cyl += 1

        # We dynamically build the Track-LUT and -Data arrays.
        tlut = bytearray()
        tdat = bytearray()

        # Empty disk may have no bitrate
        if self.opts.bitrate is None:
            assert not self.to_track
            self.opts.bitrate = 250

        # Stuff real data into the image.
        for i in range(n_cyl):
            s0 = self.to_track[i,0] if (i,0) in self.to_track else None
            s1 = self.to_track[i,1] if (i,1) in self.to_track else None
            if s0 is None and s1 is None:
                # Dummy data for empty cylinders. Assumes 300RPM.
                nr_bytes = 100 * self.opts.bitrate
                tlut += struct.pack("<2H", len(tdat)//512 + 2, nr_bytes)
                tdat += bytes([0x88] * (nr_bytes+0x1ff & ~0x1ff))
            else:
                # At least one side of this cylinder is populated.
                if s1 is not None:
                    n_side = 2
                bc = [s0.to_hfe_bytes() if s0 is not None else bytes(),
                      s1.to_hfe_bytes() if s1 is not None else bytes()]
                nr_bytes = max(len(t) for t in bc)
                nr_blocks = (nr_bytes + 0xff) // 0x100
                tlut += struct.pack("<2H", len(tdat)//512 + 2, 2 * nr_bytes)
                for b in range(nr_blocks):
                    for t in bc:
                        slice = t[b*256:(b+1)*256]
                        tdat += slice + bytes([0x88] * (256 - len(slice)))

        # Construct the image header.
        header = struct.pack("<8s4B2H2BH",
                             b"HXCPICFE",
                             0,
                             n_cyl,
                             n_side,
                             0xff, # unknown encoding
                             self.opts.bitrate,
                             0,    # rpm (unused)
                             0xff, # unknown interface
                             1,    # rsvd
                             1)    # track list offset

        # Pad the header and TLUT to 512-byte blocks.
        header += bytes([0xff] * (0x200 - len(header)))
        tlut += bytes([0xff] * (0x200 - len(tlut)))

        return header + tlut + tdat


# Local variables:
# python-indent: 4
# End:
