"""Run makemkvcon in robot mode (-r) and parse its output.

Robot lines look like  KIND:field,field,"quoted, field"  e.g.
  CINFO:2,0,"HOME_ALONE"          disc attribute 2 (name)
  TINFO:0,9,0,"1:42:44"           title 0, attribute 9 (duration)
  PRGV:12000,30000,65536          current, total, max progress
  PRGT:5018,0,"Saving to MKV file" / PRGC:... total / current task name
  MSG:code,flags,nparams,"message","format",params...
"""
import csv
import logging
import re
import subprocess
import time

log = logging.getLogger("makemkv")

# ap_ItemAttributeId values (makemkvgui/inc/lgpl/apdefs.h)
A_NAME, A_CHAPTERS, A_DURATION, A_SIZE_BYTES = 2, 8, 9, 11
# Per-title chatter during scans (hundreds of lines on discs with many short
# titles), matched on the MSG format string.
NOISY_MSGS = re.compile(r"less than minimum title length|is identical to stream|"
                        r"looks empty and was skipped|were removed from title")
A_SOURCE_FILE, A_SEGMENTS_MAP, A_OUTPUT_FILE, A_VOLUME_NAME = 16, 26, 27, 32


def parse_line(line):
    """'KIND:a,b,"c"' -> ('KIND', ['a', 'b', 'c']); (None, []) if not a robot line."""
    kind, sep, rest = line.rstrip("\n").partition(":")
    if not sep or not kind.isupper():
        return None, []
    return kind, next(csv.reader([rest]), [])


def hms_to_s(text):
    s = 0
    for part in text.split(":"):
        s = s * 60 + int(part)
    return s


class Info:
    """Disc and title attributes collected from CINFO/TINFO lines."""

    def __init__(self):
        self.disc = {}
        self.titles = {}
        self.messages = []
        self.version = None

    def feed(self, kind, f):
        if kind == "CINFO":
            self.disc[int(f[0])] = f[2]
        elif kind == "TINFO":
            self.titles.setdefault(int(f[0]), {})[int(f[1])] = f[3]
        elif kind == "MSG":
            self.messages.append((int(f[0]), f[3]))
            if f[0] == "1005":
                self.version = f[3]

    @property
    def name(self):
        return self.disc.get(A_NAME) or self.disc.get(A_VOLUME_NAME)

    def title_list(self):
        """[{index, duration_s, size, chapters, source, output}] in disc title order."""
        out = []
        for i, a in sorted(self.titles.items()):
            out.append({
                "index": i,
                "duration_s": hms_to_s(a.get(A_DURATION, "0")),
                "size": int(a.get(A_SIZE_BYTES, 0)),
                "chapters": int(a.get(A_CHAPTERS, 0)),
                "source": a.get(A_SOURCE_FILE),
                "segments": a.get(A_SEGMENTS_MAP),
                "output": a.get(A_OUTPUT_FILE),
            })
        return out


def run(args, on_line=None, timeout=None):
    """Run makemkvcon -r ARGS, feeding (kind, fields) of each line to on_line.

    Every raw line is logged at DEBUG; MSG lines at INFO. Returns the exit code,
    or None on timeout.
    The process is killed if this function is interrupted (e.g. by a cancel).
    """
    cmd = ["makemkvcon", "-r", "--progress=-same", *map(str, args)]
    log.info("run: %s", " ".join(cmd))
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    rc = None
    try:
        for line in proc.stdout:
            kind, fields = parse_line(line)
            if kind == "MSG" and len(fields) > 3:
                level = logging.DEBUG if NOISY_MSGS.search(fields[4]) else logging.INFO
                log.log(level, "  %s", fields[3])
            else:
                log.debug("  %s", line.rstrip())
            if kind and on_line:
                on_line(kind, fields)
            if timeout and time.monotonic() - t0 > timeout:
                log.error("makemkvcon timed out after %ss", timeout)
                break
        else:
            rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    log.info("makemkvcon rc=%s in %.0fs", rc, time.monotonic() - t0)
    return rc


def failed_to_open(info):
    return any(re.search(r"failed to open disc", m, re.I) for _, m in info.messages)
