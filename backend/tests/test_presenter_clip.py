"""The agent's presenting clip: what we accept, what we refuse, and why.

This runs real ffmpeg against real generated video, because the whole point of
transcoding on upload is that a file which cannot be read fails while the agent is
still looking at the screen -- not three days later inside someone's video.
"""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_renderer as vr

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


def streaminfo(path):
    """codec, width, height -- read from ffmpeg's own stderr, so no ffprobe needed."""
    err = subprocess.run(["ffmpeg","-hide_banner","-i",path], capture_output=True).stderr.decode("utf-8","replace")
    m = re.search(r"Video: (\w+).*?, (\d+)x(\d+)", err, re.S)
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else (None, 0, 0)

def make(path, seconds, w, h, codec="libx264", with_audio=True):
    cmd = ["ffmpeg","-y","-v","error","-f","lavfi","-i",
           "testsrc=size=%dx%d:rate=30:duration=%s" % (w,h,seconds)]
    if with_audio:
        cmd += ["-f","lavfi","-i","sine=frequency=440:duration=%s" % seconds, "-c:a","aac"]
    cmd += ["-c:v",codec,"-t",str(seconds),"-pix_fmt","yuv420p",path]
    subprocess.run(cmd, check=True, capture_output=True)

# The renderer calls plain "ffmpeg" from PATH, which Railway has and this laptop does
# not. Borrow imageio-ffmpeg's bundled binary and put it on PATH under that name, so
# these tests actually RUN locally instead of skipping and telling us nothing.
if subprocess.run(["which","ffmpeg"], capture_output=True).returncode != 0:
    try:
        import imageio_ffmpeg, shutil
        shim = tempfile.mkdtemp()
        shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), os.path.join(shim, "ffmpeg"))
        os.environ["PATH"] = shim + os.pathsep + os.environ["PATH"]
        print("(using imageio-ffmpeg's binary; note ffprobe is absent, so duration "
              "falls back -- which is itself worth proving)")
    except Exception as e:
        print("no ffmpeg available (%s) — skipping" % e); sys.exit(0)

with tempfile.TemporaryDirectory() as tmp:
    print("\n[1] a normal phone-shaped clip is accepted and normalised")
    src = os.path.join(tmp, "portrait.mp4"); make(src, 6, 720, 1280)
    data, secs = vr.prepare_presenter_clip(open(src,"rb").read(), tmp)
    check("returns bytes", isinstance(data, bytes) and len(data) > 1000, str(len(data)))
    check("a duration is returned", secs > 0, "%.2f" % secs)
    out = os.path.join(tmp,"o.mp4"); open(out,"wb").write(data)
    codec, w, h = streaminfo(out)
    check("re-encoded to h264 (got %s)" % codec, codec == "h264")
    check("long edge capped at %d (got %dx%d)" % (vr.PRESENTER_LONG_EDGE, w, h),
          max(w,h) <= vr.PRESENTER_LONG_EDGE)
    check("dimensions stayed even (h264 requires it)", w % 2 == 0 and h % 2 == 0, "%dx%d"%(w,h))

    print("\n[2] an over-long recording is trimmed, not rejected")
    src2 = os.path.join(tmp,"long.mp4"); make(src2, 20, 640, 360)
    _d2, secs2 = vr.prepare_presenter_clip(open(src2,"rb").read(), tmp)
    check("never reports longer than the cap", secs2 <= vr.PRESENTER_MAX_SECONDS + 0.6, "%.2f" % secs2)
    o2 = os.path.join(tmp,"o2.mp4"); open(o2,"wb").write(_d2)
    real = subprocess.run(["ffmpeg","-v","error","-i",o2,"-f","null","-"],capture_output=True)
    check("the trimmed file itself is readable", real.returncode == 0)

    print("\n[3] landscape is handled too, not just portrait")
    src3 = os.path.join(tmp,"wide.mp4"); make(src3, 4, 1920, 1080)
    d3, _ = vr.prepare_presenter_clip(open(src3,"rb").read(), tmp)
    open(os.path.join(tmp,"o3.mp4"),"wb").write(d3)
    _, w3, h3 = streaminfo(os.path.join(tmp,"o3.mp4"))
    check("landscape long edge capped (%dx%d)" % (w3,h3), max(w3,h3) <= vr.PRESENTER_LONG_EDGE)

    print("\n[4] rubbish is refused with a sentence the agent can act on")
    try:
        vr.prepare_presenter_clip(b"this is not a video at all, not even close", tmp)
        check("a non-video raises", False, "it was accepted")
    except ValueError as e:
        check("a non-video raises ValueError", True)
        check("the message is plain English, not a stack trace",
              "video" in str(e).lower() and len(str(e)) < 200, str(e)[:90])
    except Exception as e:
        check("a non-video raises ValueError (got %s)" % type(e).__name__, False)

    print("\n[5] a mis-tap (very short) is refused rather than silently used")
    src5 = os.path.join(tmp,"tiny.mp4"); make(src5, 0.6, 320, 240, with_audio=False)
    try:
        vr.prepare_presenter_clip(open(src5,"rb").read(), tmp)
        check("a sub-second clip is refused", False, "it was accepted")
    except ValueError as e:
        check("a sub-second clip is refused", True)
        check("and says what to do instead", "second" in str(e).lower(), str(e)[:80])

print("\n" + "="*60)
print("PASSED: %d    FAILED: %d" % (P, F))
print("="*60)
sys.exit(1 if F else 0)
