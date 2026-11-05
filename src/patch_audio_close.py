#!/usr/bin/env python3
"""patch_audio_close.py -- ISSUE-68 fix, applied to a copy of the stock
usr/sys/amiga/driver/audio.c (AMIX 2.1c).  Usage: patch_audio_close.py audio.c

The source is Commodore's and is not in this repository; this script edits a
build-time copy and asserts the stock text before every change.

1. audioclose: replace the body.  The stock close frees the client while
   audio_channel[].client may still point at it, so the next audio interrupt
   runs getq()/fill_buffer() on freed memory (a panic in audiointr).  The new
   close waits for the queue to drain (the interrupt wakes it via
   AUD_CCLOSING), then -- drained or interrupted by a signal -- silences and
   detaches every channel the client holds, frees the partial message, and
   unlinks it with a termination check, all at splaudio(), before kmem_free.
2. audiointr: when a waiting client's message is only partly posted, the
   stock code also stored it in the PREVIOUS list entry's mp; with two
   clients that frees it twice.  The rest stays in acl->next->mp.
"""
import sys

p = sys.argv[1]
s = open(p, encoding="latin-1").read()

a = s.find("static int\naudioclose(queue_t *q)\n{")
b = s.find("static int\naudiowput(struct queue *q, mblk_t *mp)")
if a < 0 or b < a or "#if 0" not in s[a:b] or "kmem_free(acl" not in s[a:b]:
    sys.exit("[FAIL] audio.c: stock audioclose not found as expected")
s = s[:a] + '''static int
audioclose(queue_t *q)
{
    struct audio_client *acl = (struct audio_client *)q->q_ptr, *aclp;
    int s, i;

    /* AMIX 68040/68060 port, ISSUE-68: never free acl while an audio
    ** channel can still reach it from the interrupt. */
    s = splaudio();

    /* Let what is queued play out; audiointr detaches the client and
    ** wakes us.  A signal (^C, kill) ends the wait. */
    while (acl->mp != NULL || WR(q)->q_first != NULL || (acl->flags & AUD_CATTACHED))
    {
	acl->flags |= AUD_CCLOSING;
	if (sleep((caddr_t)acl, PZERO+1|PCATCH))
	    break;
    }
    acl->flags &= ~AUD_CCLOSING;

    /* Whatever is left: silence and detach this client's channels. */
    for (i = 0; i < MAXCHANNELS; ++i)
    {
	if (audio_channel[i].client == acl)
	{
	    AMIGA->intena = AINTCLR | audio_channel[i].dmadone;
	    AMIGA->dmacon = DMACLR | audio_channel[i].enable;
	    AMIGA->intreq = AINTCLR | audio_channel[i].dmadone;
	    audio_channel[i].client = NULL;
	    audio_channel[i].pending = 0;
	    audio_channel[i].used = 0;
	    audio_channel[i].nbytes[0] = audio_channel[i].nbytes[1] =
		audio_channel[i].nbytes[2] = 0;
	}
    }
    acl->flags &= ~AUD_CATTACHED;
    if (acl->mp)
    {
	freemsg(acl->mp);
	acl->mp = NULL;
    }
    acl->q = NULL;

    /* Unlink -- only if it is on the list. */
    for (aclp = &audio_clients; aclp->next != NULL && aclp->next != acl; aclp = aclp->next)
	;
    if (aclp->next == acl)
	aclp->next = acl->next;

    splx(s);

    kmem_free(acl, sizeof(*acl));

    q->q_ptr = NULL;
    WR(q)->q_ptr = NULL;

    return 0;
}

''' + s[b:]
print("      [ok] audioclose: drain wait, channels detached, guarded unlink")

old = """		if (postaudio(acl->next->q, acl->next->mp))
		{
		    acl->mp = acl->next->mp;
		}"""
if s.count(old) != 1:
    sys.exit("[FAIL] audio.c: audiointr client-list post not found as expected")
s = s.replace(old, """		if (postaudio(acl->next->q, acl->next->mp))
		{
		    /* partly posted: the rest stays in acl->next->mp */
		}""")
print("      [ok] audiointr: partly posted message no longer aliased")
open(p, "w", encoding="latin-1").write(s)
