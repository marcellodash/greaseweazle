# greaseweazle/tools/read.py
#
# Greaseweazle control script: Read Disk to Image.
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

description = "Read a disk to the specified image file."

import sys, copy

from greaseweazle.tools import util
from greaseweazle import error
from greaseweazle import usb as USB
from greaseweazle.flux import Flux
from greaseweazle.codec import formats

from greaseweazle import track
plls = track.plls

def open_image(args, image_class):
    image = image_class.to_file(
        args.file, None if args.raw else args.fmt_cls, args.no_clobber)
    for opt, val in args.file_opts.items():
        error.check(hasattr(image, 'opts') and hasattr(image.opts, opt),
                    "%s: Invalid file option: %s" % (args.file, opt))
        setattr(image.opts, opt, val)
    image.write_on_ctrl_c = True
    return image


def read_and_normalise(usb, args, revs, ticks=0):
    if args.fake_index is not None:
        drive_tpr = int(args.drive_ticks_per_rev)
        pre_index = int(usb.sample_freq * 0.5e-3)
        if ticks == 0:
            ticks = revs*drive_tpr + 2*pre_index
        flux = usb.read_track(revs=0, ticks=ticks)
        flux.index_list = ([pre_index] +
                           [drive_tpr] * ((ticks-pre_index)//drive_tpr))
    else:
        flux = usb.read_track(revs=revs, ticks=ticks)
    flux._ticks_per_rev = args.drive_ticks_per_rev
    if args.adjust_speed is not None:
        flux.scale(args.adjust_speed / flux.time_per_rev)
    return flux


def read_with_retry(usb, args, t, decoder):

    cyl, head = t.cyl, t.head

    usb.seek(t.physical_cyl, t.physical_head)

    flux = read_and_normalise(usb, args, args.revs, args.ticks)
    if decoder is None:
        print("T%u.%u: %s" % (cyl, head, flux.summary_string()))
        return flux, flux

    dat = decoder(cyl, head, flux)
    if dat is None:
        print("T%u.%u: WARNING: Out of range for for format '%s': No format "
              "conversion applied" % (cyl, head, args.format))
        return flux, None
    for pll in plls[1:]:
        if dat.nr_missing() == 0:
            break
        dat.decode_raw(flux, pll)

    seek_retry, retry = 0, 0
    while True:
        s = "T%u.%u: %s from %s" % (cyl, head, dat.summary_string(),
                                    flux.summary_string())
        if retry != 0:
            s += " (Retry #%u.%u)" % (seek_retry, retry)
        print(s)
        if dat.nr_missing() == 0:
            break
        if (retry % args.retries) == 0:
            if seek_retry > args.seek_retries:
                print("T%u.%u: Giving up: %d sectors missing"
                      % (cyl, head, dat.nr_missing()))
                break
            if retry != 0:
                usb.seek(0, 0)
                usb.seek(t.physical_cyl, t.physical_head)
            seek_retry += 1
            retry = 0
        retry += 1
        _flux = read_and_normalise(usb, args, max(args.revs, 3))
        for pll in plls:
            if dat.nr_missing() == 0:
                break
            dat.decode_raw(_flux, pll)
        flux.append(_flux)

    return flux, dat


def print_summary(args, summary):
    if not summary:
        return
    s = 'Cyl-> '
    p = -1
    for c in args.tracks.cyls:
        s += ' ' if c//10==p else str(c//10)
        p = c//10
    print(s)
    s = 'H. S: '
    for c in args.tracks.cyls:
        s += str(c%10)
    print(s)
    tot_sec = good_sec = 0
    for head in args.tracks.heads:
        nsec = max((summary[x].nsec for x in summary if x[1] == head),
                   default = None)
        if nsec is None:
            continue
        for sec in range(nsec):
            print("%d.%2d: " % (head, sec), end="")
            for cyl in args.tracks.cyls:
                s = summary.get((cyl,head), None)
                if s is None or sec >= s.nsec:
                    print(" ", end="")
                else:
                    tot_sec += 1
                    if s.has_sec(sec): good_sec += 1
                    print("." if s.has_sec(sec) else "X", end="")
            print()
    if tot_sec != 0:
        print("Found %d sectors of %d (%d%%)" %
              (good_sec, tot_sec, good_sec*100/tot_sec))


def read_to_image(usb, args, image, decoder=None):
    """Reads a floppy disk and dumps it into a new image file.
    """

    args.ticks, args.drive_ticks_per_rev = 0, None

    if args.fake_index is not None:
        args.drive_ticks_per_rev = args.fake_index * usb.sample_freq

    if isinstance(args.revs, float):
        if args.raw:
            # If dumping raw flux we want full index-to-index revolutions.
            args.revs = 2
        else:
            # Measure drive RPM.
            # We will adjust the flux intervals per track to allow for this.
            if args.drive_ticks_per_rev is None:
                args.drive_ticks_per_rev = usb.read_track(2).ticks_per_rev
            args.ticks = int(args.drive_ticks_per_rev * args.revs)
            args.revs = 2

    summary = dict()

    for t in args.tracks:
        cyl, head = t.cyl, t.head
        flux, dat = read_with_retry(usb, args, t, decoder)
        if decoder is not None and dat is not None:
            summary[cyl,head] = dat
        if args.raw:
            image.emit_track(cyl, head, flux)
        elif dat is not None:
            image.emit_track(cyl, head, dat)

    if decoder is not None:
        print_summary(args, summary)


def main(argv):

    epilog = (util.drive_desc + "\n"
              + util.speed_desc + "\n" + util.tspec_desc
              + "\n" + util.pllspec_desc
              + "\nFORMAT options:\n" + formats.print_formats())
    parser = util.ArgumentParser(usage='%(prog)s [options] file',
                                 epilog=epilog)
    parser.add_argument("--device", help="device name (COM/serial port)")
    parser.add_argument("--drive", type=util.drive_letter, default='A',
                        help="drive to read")
    parser.add_argument("--diskdefs", help="disk definitions file")
    parser.add_argument("--format", help="disk format (output is converted unless --raw)")
    parser.add_argument("--revs", type=int, metavar="N",
                        help="number of revolutions to read per track")
    parser.add_argument("--tracks", type=util.TrackSet, metavar="TSPEC",
                        help="which tracks to read")
    parser.add_argument("--raw", action="store_true",
                        help="output raw stream (--format verifies only)")
    parser.add_argument("--fake-index", type=util.period, metavar="SPEED",
                        help="fake index pulses at SPEED")
    parser.add_argument("--adjust-speed", type=util.period, metavar="SPEED",
                        help="scale track data to effective drive SPEED")
    parser.add_argument("--retries", type=int, default=3, metavar="N",
                        help="number of retries per seek-retry")
    parser.add_argument("--seek-retries", type=int, default=0, metavar="N",
                        help="number of seek retries")
    parser.add_argument("-n", "--no-clobber", action="store_true",
                        help="do not overwrite an existing file")
    parser.add_argument("--pll", type=track.PLL, metavar="PLLSPEC",
                        help="manual PLL parameter override")
    parser.add_argument("file", help="output filename")
    parser.description = description
    parser.prog += ' ' + argv[1]
    args = parser.parse_args(argv[2:])

    args.file, args.file_opts = util.split_opts(args.file)

    if args.pll is not None:
        plls.insert(0, args.pll)

    try:
        usb = util.usb_open(args.device)
        image_class = util.get_image_class(args.file)
        if not args.format and hasattr(image_class, 'default_format'):
            args.format = image_class.default_format
        decoder, def_tracks, args.fmt_cls = None, None, None
        if args.format:
            args.fmt_cls = formats.get_format(args.format, args.diskdefs)
            if args.fmt_cls is None:
                raise error.Fatal("""\
Unknown format '%s'
Known formats:\n%s"""
                                  % (args.format, formats.print_formats(
                                      args.diskdefs)))
            decoder = args.fmt_cls.decode_track
            def_tracks = copy.copy(args.fmt_cls.tracks)
            if args.revs is None: args.revs = args.fmt_cls.default_revs
        if def_tracks is None:
            def_tracks = util.TrackSet('c=0-81:h=0-1')
        if args.revs is None: args.revs = 3
        if args.tracks is not None:
            def_tracks.update_from_trackspec(args.tracks.trackspec)
        args.tracks = def_tracks
        
        print(("Reading %s revs=" % args.tracks) + str(args.revs))
        if args.format:
            print("Format " + args.format)
        with open_image(args, image_class) as image:
            util.with_drive_selected(read_to_image, usb, args, image,
                                     decoder=decoder)
    except USB.CmdError as err:
        print("Command Failed: %s" % err)


if __name__ == "__main__":
    main(sys.argv)

# Local variables:
# python-indent: 4
# End:
