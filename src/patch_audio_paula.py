#!/usr/bin/env python3
"""patch_audio_paula.py -- /dev/audio and /dev/paula exclude each other.
Applied after patch_audio_stereo.py to the build-time copy of audio.c.
Usage: patch_audio_paula.py audio.c

/dev/paula (src/paula.c, major 53) takes Paula's four channels and drives
their registers directly; /dev/audio feeds the same channels from its own
buffers and interrupts.  They must never run at once.  This adds two
globals to audio.c, which is linked before paula.o:

  paula_busy   set by paulaopen(); audioopen() then fails with EBUSY
  audio_nopen  /dev/audio clients now open; paulaopen() fails with EBUSY
               while it is not zero

The count goes up where a client joins the client list in audioopen() and
down where it leaves the list in audioclose().  Both run in process
context on a uniprocessor kernel, so a plain int is enough.

The source is Commodore's and is not in this repository; every edit
asserts the text it replaces.
"""
import sys

path = sys.argv[1]
s = open(path, encoding="latin-1").read()


def sub(s, old, new, what):
    n = s.count(old)
    if n != 1:
        sys.exit(f"[FAIL] audio.c: {what}: found {n} times, expected 1")
    return s.replace(old, new)


if "paula_busy" in s:
    sys.exit("[FAIL] audio.c already carries the /dev/paula exclusion")

s = sub(s, "static struct audio_client audio_clients;\n",
        "static struct audio_client audio_clients;\n"
        "int paula_busy;\t\t/* /dev/paula holds the channels (src/paula.c) */\n"
        "int audio_nopen;\t/* clients on the list: /dev/paula waits for 0 */\n",
        "audio_clients declaration")
s = sub(s, "    if (sflag == MODOPEN)\n\treturn EINVAL;\n",
        "    if (sflag == MODOPEN)\n\treturn EINVAL;\n"
        "    if (paula_busy)\n\treturn EBUSY;\n",
        "audioopen MODOPEN test")
s = sub(s, "    audio_clients.next = acl;\n    splx(s);\n",
        "    audio_clients.next = acl;\n    audio_nopen++;\n    splx(s);\n",
        "audioopen list insert")
s = sub(s, "\taclp->next = acl->next;\n\n    splx(s);\n",
        "\taclp->next = acl->next;\n    audio_nopen--;\n\n    splx(s);\n",
        "audioclose unlink")
open(path, "w", encoding="latin-1").write(s)
print("      [ok] audio.c: paula_busy / audio_nopen exclusion with /dev/paula")
