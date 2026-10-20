"""The presenting clip must keep the seconds the agent is actually TALKING.

People do not start speaking the instant they tap record. Measured on Janel's own
recording: silent for the first 2.4s, speaking from there to about 9s. Keeping the first
SECONDS_PER_PHOTO from zero would have opened her Signature with two seconds of her
standing there saying nothing and then cut her off mid-sentence -- the wrong five seconds
of a perfectly good take.

The threshold for "quiet" is relative to the clip's own loudest moment, NOT a fixed dB
number. That is the point of [4]: a fixed -45dB was tried first and never fired on
Janel's clip, because her room's noise floor peaks at -42dB. A fixed -40dB would have
worked on that one recording and been tuned to that one room.

Needs ffmpeg on PATH; skips cleanly without it. Never needs ffprobe (a worker can have a
perfectly good ffmpeg and no ffprobe).
"""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_renderer as vr

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))

try:
    subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, timeout=20)
except Exception:
    print("ffmpeg is not on PATH; skipping"); sys.exit(0)

TMP = tempfile.mkdtemp()

def clip(name, seconds, speech_from=None, speech_to=None, floor_db=-50):
    """A fake recording: a constant noise floor, with a 'voice' over part of it."""
    path = os.path.join(TMP, name + ".mp4")
    if speech_from is None:
        voice = "anullsrc=r=44100:cl=mono"
    else:
        to = seconds if speech_to is None else speech_to
        voice = (f"sine=frequency=220:r=44100,"
                 f"volume='between(t,{speech_from},{to})':eval=frame")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=360x640:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"anoisesrc=r=44100:c=pink:a={10 ** (floor_db / 20.0):.6f}",
        "-f", "lavfi", "-i", voice,
        "-filter_complex", f"[1:a][2:a]amix=inputs=2:duration=first:normalize=0,"
                           f"atrim=0:{seconds},asetpts=PTS-STARTPTS[a]",
        "-map", "0:v", "-map", "[a]", "-t", str(seconds),
        "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", path,
    ], check=True, capture_output=True)
    return path

def start_of(path):
    return vr._speech_start_seconds(path, vr._duration_via_ffmpeg(path), vr.SECONDS_PER_PHOTO)

print("\n[1] a clip that opens with dead air is trimmed to the first word")
p = clip("lead_in", 12, speech_from=2.4)
s = start_of(p)
check("trims to about where the speech starts", 1.8 < s < 2.5, "got %.2fs" % s)
check("keeps a lead-in rather than clipping the first syllable", s < 2.4, "got %.2fs" % s)

print("\n[2] a clip where they start talking straight away is not trimmed")
p = clip("immediate", 12, speech_from=0)
s = start_of(p)
check("left alone", s == 0.0, "got %.2fs" % s)

print("\n[3] a clip with no speech at all is kept whole, not refused")
p = clip("silent", 12)
s = start_of(p)
check("left alone", s == 0.0, "got %.2fs" % s)

print("\n[4] the threshold is relative to the clip, not a fixed dB number")
# A loud room: the noise floor here is ABOVE the fixed -45dB threshold that was tried
# first, which is exactly why that version never fired on Janel's real recording.
p = clip("noisy_room", 12, speech_from=2.4, floor_db=-40)
s = start_of(p)
check("still finds the start of speech in a noisy room", 1.8 < s < 2.5, "got %.2fs" % s)

print("\n[5] never trims so far that less than one slide is left")
# Speaks only in the last 2 seconds of a 10s clip. Trimming to the speech would leave
# 2s, and the film needs SECONDS_PER_PHOTO to open with.
p = clip("late_talker", 10, speech_from=8)
s = start_of(p)
check("clamped so a full slide remains", s <= 10 - vr.SECONDS_PER_PHOTO + 0.01, "got %.2fs" % s)
check("and the clip still has a full slide in it", 10 - s >= vr.SECONDS_PER_PHOTO - 0.01)

print("\n[6] anything unreadable keeps the clip from the beginning")
check("unknown duration -> no trim", vr._speech_start_seconds(
    clip("fine", 8, speech_from=2.4), None, vr.SECONDS_PER_PHOTO) == 0.0)
check("zero duration -> no trim", vr._speech_start_seconds(
    os.path.join(TMP, "fine.mp4"), 0, vr.SECONDS_PER_PHOTO) == 0.0)
bad = os.path.join(TMP, "not_a_video.mp4")
open(bad, "wb").write(b"this is not a video")
check("a file ffmpeg cannot read -> no trim, no raise",
      vr._speech_start_seconds(bad, 12.0, vr.SECONDS_PER_PHOTO) == 0.0)

print("\n[7] end to end: the stored clip starts on the speech")
raw = open(clip("upload", 12, speech_from=2.4), "rb").read()
with tempfile.TemporaryDirectory() as wd:
    data, seconds, _note = vr.prepare_presenter_clip(raw, wd)
out = os.path.join(TMP, "prepared.mp4")
open(out, "wb").write(data)
check("the dead air is gone from what we store", seconds < 10.5, "%.2fs stored" % seconds)
check("and there is still at least a full slide of it",
      seconds >= vr.SECONDS_PER_PHOTO, "%.2fs stored" % seconds)
err = subprocess.run(["ffmpeg", "-hide_banner", "-ss", "0", "-t", "1", "-i", out,
                      "-af", "volumedetect", "-f", "null", "-"],
                     capture_output=True).stderr.decode("utf-8", "replace")
import re
first_second = float(re.search(r"mean_volume:\s*(-?[\d.]+) dB", err).group(1))
check("the first second of the stored clip is speech, not room tone",
      first_second > -48, "first second measured %.1f dB" % first_second)

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
