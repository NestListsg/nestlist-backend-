"""Colour must never cost the video.

The opening slide is the one stage of a Signature that cannot degrade -- a film without
the agent in it is a Classic -- so anything that can FAIL there fails the whole render
and tells the agent their clip could not be read.

That is not hypothetical. The first production Signature rendered after the colour fix
shipped did exactly this: Railway's ffmpeg refused the repair chain, and an agent with a
perfectly good recording was told to go and record another one. A probe saying a filter
exists is not a promise that a particular graph will run.

So the repair and the colour tags -- both cosmetic -- are attempted in descending order
of niceness, and the last rung is byte-for-byte the command that shipped before any of
the colour work existed.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_renderer as vr

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))

TMP = tempfile.mkdtemp()

HDR = {"transfer": "arib-std-b67", "matrix": "bt2020nc",
       "primaries": "bt2020", "range": "tv"}


def run(fail_when, colour):
    """Drive _presenter_opening_clip with ffmpeg stubbed. `fail_when(cmd)` decides which
    commands are refused. Returns (commands attempted, note, raised)."""
    cmds = []

    def fake_run_ffmpeg(cmd, timeout):
        cmds.append(list(cmd))
        if fail_when(cmd):
            raise RuntimeError("ffmpeg exited 1: injected refusal")
        with open(cmd[-1], "wb") as f:
            f.write(b"\0" * 2048)

    saved = (vr._run_ffmpeg, vr._source_colour)
    vr._run_ffmpeg = fake_run_ffmpeg
    vr._source_colour = lambda path: dict(colour)
    try:
        out = os.path.join(TMP, "slide.mp4")
        note = vr._presenter_opening_clip("in.mp4", out)
        return cmds, note, None
    except Exception as e:
        return cmds, None, e
    finally:
        vr._run_ffmpeg, vr._source_colour = saved


def graph(cmd):
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""

def tagged(cmd):
    return "-x264-params" in cmd

never = lambda cmd: False

print("\n[1] a healthy HDR clip is converted and tagged, in one attempt")
cmds, note, err = run(never, HDR)
check("no exception", err is None, repr(err))
check("one ffmpeg call", len(cmds) == 1, str(len(cmds)))
check("the repair is in the graph", "tonemap" in graph(cmds[0]) or "colorspace" in graph(cmds[0])
      or graph(cmds[0]).startswith("[0:v]fps="), graph(cmds[0])[:80])
check("the slide is tagged bt709", tagged(cmds[0]))

print("\n[2] if the repair chain is refused, the film is still made")
# This is the production failure: the probe said the filter existed, the graph was
# refused anyway.
repair_in = lambda cmd: ("tonemap" in graph(cmd) or "zscale" in graph(cmd)
                         or "colorspace" in graph(cmd))
cmds, note, err = run(repair_in, HDR)
check("no exception -- the agent still gets their video", err is None, repr(err))
check("it fell back rather than giving up", len(cmds) >= 2, str(len(cmds)))
check("the surviving command carries no repair",
      not repair_in(cmds[-1]), graph(cmds[-1])[:80])
check("but it is still tagged bt709, so the card is right", tagged(cmds[-1]))
check("and the agent is told the colour is not fully corrected",
      note == vr.VIDEO_NOTE_HDR_NOT_TONEMAPPED, repr(note))

print("\n[3] if the colour tags are refused too, it still makes the film")
cmds, note, err = run(lambda cmd: repair_in(cmd) or tagged(cmd), HDR)
check("no exception", err is None, repr(err))
check("three attempts, worst last", len(cmds) == 3, str(len(cmds)))
check("the last command is the pre-colour-fix one",
      not tagged(cmds[-1]) and not repair_in(cmds[-1]))
check("still reported as not fully corrected",
      note == vr.VIDEO_NOTE_HDR_NOT_TONEMAPPED, repr(note))

print("\n[4] an ordinary SDR clip never attempts a repair")
SDR = {"transfer": "bt709", "matrix": "bt709", "primaries": "bt709", "range": "tv"}
cmds, note, err = run(never, SDR)
check("no exception", err is None, repr(err))
check("one call", len(cmds) == 1, str(len(cmds)))
check("no repair in the graph", not repair_in(cmds[0]), graph(cmds[0])[:80])
check("no degradation reported", note is None, repr(note))

print("\n[5] a genuinely unreadable clip still raises, as it must")
cmds, note, err = run(lambda cmd: True, HDR)
check("raises after exhausting every rung", err is not None)
check("tried every rung first", len(cmds) == 3, str(len(cmds)))

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
