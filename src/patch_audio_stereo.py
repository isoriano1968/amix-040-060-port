#!/usr/bin/env python3
"""patch_audio_stereo.py -- sample-locked stereo for the AMIX audio driver,
applied after patch_audio_close.py to the build-time copies of audio.c and
audio.h.  Usage: patch_audio_stereo.py audio.c audio.h

The stock driver gives every open() one Paula channel.  A stereo player that
opens /dev/audio twice gets two channels that each start -- and restart after
any underrun -- on their own, so left and right drift apart by milliseconds:
an audible phasing sweep.  Commodore's code already carries two channel slots
per client (ports[2], ach[2]) but only ever uses the first.

New: ioctl AUDIO_STEREO (int 1/0, before the first write).  A stereo client
writes interleaved L,R signed bytes; it gets one left channel (0 or 3) and one
right channel (1 or 2).  Both are hand-fed together and started by ONE dmacon
write, run at the same period from equal-length buffers, and only the left
channel interrupts: each left interrupt services both.  So the pair cannot
drift.  audio_channel[left].partner = 1 + right while the pair (or its
silent tail) is live; a right channel that is a live partner is not free.

Also fixed (stock defect): when audiointr copied only part of a message into
a DMA buffer it did not advance b_rptr, so the next refill copied the same
bytes again (postaudio does advance).

Also fixed (stock defect): AUDIO_VOLUME and AUDIO_PERIOD answered the ioctl
and then "break" fell through to the common ACK path, which acknowledged the
same message again -- a second putnext() of a block already sent upstream, on
every ioctl.  Those cases now return.  Their own ACK never set ioc_error and
ioc_rval, which in an M_IOCDATA block overlay the copyresp's cp_pad1 and
cp_private (= 1): only the second ACK made the ioctl succeed.  They are set
now (without that, every AUDIO_PERIOD returned a garbage error).

The source is Commodore's and is not in this repository; every edit asserts
the text it replaces.
"""
import sys

cpath, hpath = sys.argv[1], sys.argv[2]
c = open(cpath, encoding="latin-1").read()
h = open(hpath, encoding="latin-1").read()


def sub(s, old, new, count=1, what=""):
    n = s.count(old)
    if n != count:
        sys.exit(f"[FAIL] {what or old[:50]!r}: found {n} times, expected {count}")
    return s.replace(old, new)


def between(s, start, end, new, what):
    a = s.find(start)
    b = s.find(end, a + 1)
    if a < 0 or b < 0:
        sys.exit(f"[FAIL] {what}: anchors not found")
    return s[:a] + new + s[b:]


# ---- audio.h ------------------------------------------------------------
h = sub(h, "    struct audio_client *next;\n};",
        "    struct audio_client *next;\n"
        "    int\t\t\tstereo;\t\t/* AUDIO_STEREO: ports[0] left, ports[1] right */\n};",
        what="audio_client")
h = sub(h, "    signed char\t\t\t*buffer[3]; /* data for audio */\n};",
        "    signed char\t\t\t*buffer[3]; /* data for audio */\n"
        "    int\t\t\t\tpartner; /* stereo left: 1 + index of its right channel */\n};",
        what="audio_channel")
h = sub(h, "#define AUDIO_PERIOD\t\t(AUIOC|2)\n",
        "#define AUDIO_PERIOD\t\t(AUIOC|2)\n"
        "#define AUDIO_STEREO\t\t(AUIOC|3)\t/* int: 1 = interleaved L,R bytes */\n",
        what="ioctl numbers")

# ---- audio.c: small edits -----------------------------------------------
c = sub(c, "static signed char audio_silent_buffer[2];",
        "static signed char audio_silent_buffer[4];\t/* 2 bytes per channel */")
c = sub(c, "static void release_channel_buffer(struct audio_channel *);\n",
        "static void release_channel_buffer(struct audio_channel *);\n"
        "static int chan_free(int);\n"
        "static void stop_channel(struct audio_channel *);\n")
c = sub(c, "    acl->repeat = 0;\n", "    acl->repeat = 0;\n    acl->stereo = 0;\n", what="audioopen")
# audioclose (from patch_audio_close.py): forget the pair as well
c = sub(c, "\t    audio_channel[i].client = NULL;\n\t    audio_channel[i].pending = 0;\n",
        "\t    audio_channel[i].client = NULL;\n\t    audio_channel[i].partner = 0;\n"
        "\t    audio_channel[i].pending = 0;\n", what="audioclose detach")

# ---- ioctl: no second ACK; AUDIO_STEREO -----------------------------------
c = sub(c, "\t\t    iocbp->ioc_count = 0;\n\t\t    putnext(RD(q), mp);\n\t\t    break;\n",
        "\t\t    iocbp->ioc_count = 0;\n\t\t    putnext(RD(q), mp);\n\t\t    return;\n",
        count=2, what="ioctl NAK paths")
c = sub(c, "\t\tiocbp->ioc_count = 0;\n\t\tputnext(RD(q), mp);\n\n\t    }\n\t    break;\n\t}\n",
        "\t\tiocbp->ioc_count = 0;\n"
        "\t\t/* the M_IOCDATA block still holds the copyresp: these overlay\n"
        "\t\t** cp_pad1/cp_private, so set them (the stock second ACK did) */\n"
        "\t\tiocbp->ioc_error = 0;\n\t\tiocbp->ioc_rval = 0;\n"
        "\t\tputnext(RD(q), mp);\n\n\t    }\n"
        "\t    return;\t/* answered above: the common ACK below would send it twice */\n\t}\n",
        count=2, what="ioctl ACK paths")
c = sub(c, "    default:\n\tmp->b_datap->db_type = M_IOCNAK;",
        """    case AUDIO_STEREO:
	{
	    if (mp->b_datap->db_type == M_IOCTL &&
		iocbp->ioc_count == TRANSPARENT)
	    {
		struct copyreq *creq = (struct copyreq *)mp->b_rptr;
		mp->b_datap->db_type = M_COPYIN;
		creq->cq_addr = *(caddr_t *)mp->b_cont->b_rptr;
		mp->b_wptr = mp->b_rptr + sizeof(*creq);
		creq->cq_size = sizeof(int);
		creq->cq_flag = 0;
		creq->cq_private = (mblk_t *)1;
		putnext(RD(q), mp);
		return;
	    }
	    else
	    {
		mblk_t *bp1;
		int s;

		(void)pullupmsg(mp->b_cont, -1);
		s = splaudio();
		if (!mp->b_cont || blklen(mp->b_cont) != sizeof(int) ||
		    (audio_client->flags & AUD_CATTACHED) || audio_client->mp ||
		    WR(q)->q_first)
		{
		    splx(s);
		    /* only before the first write */
		    iocbp->ioc_error = EINVAL;
		    mp->b_datap->db_type = M_IOCNAK;
		    iocbp->ioc_count = 0;
		    putnext(RD(q), mp);
		    return;
		}
		audio_client->stereo = *(int *)mp->b_cont->b_rptr != 0;
		splx(s);

		mp->b_datap->db_type = M_IOCACK;
		bp1 = unlinkb(mp);
		if (bp1)
		    freeb(bp1);
		iocbp->ioc_count = 0;
		iocbp->ioc_error = 0;
		iocbp->ioc_rval = 0;
		putnext(RD(q), mp);
	    }
	    return;
	}

    default:
	mp->b_datap->db_type = M_IOCNAK;""", what="ioctl default")

# ---- audiointr: one interrupt services a stereo pair ---------------------
c = sub(c, """	if (reqbits & (1 << i))
	{
	    struct audio_client *acl = audio_channel[i].client;
	    /* DMA is finished for this request */

	    if (acl && acl->repeat)
	    {
		--acl->repeat;
		continue;
	    }

	    release_channel_buffer(&audio_channel[i]);
""", """	if (reqbits & (1 << i))
	{
	    struct audio_client *acl = audio_channel[i].client;
	    struct audio_channel *pr;	/* stereo: the right channel */
	    int k;
	    /* DMA is finished for this request */

	    for (k = 0; k < MAXCHANNELS; ++k)
		if (audio_channel[k].partner == i + 1)
		    break;
	    if (k < MAXCHANNELS)
		continue;		/* a right channel: its left one runs it */
	    pr = audio_channel[i].partner ? &audio_channel[audio_channel[i].partner - 1] : NULL;

	    if (acl && acl->repeat)
	    {
		--acl->repeat;
		continue;
	    }

	    release_channel_buffer(&audio_channel[i]);
	    if (pr && pr->pending > 0)
		release_channel_buffer(pr);
""", what="audiointr head")
c = sub(c, """		if (acl)	
		{
		    fill_buffer(acl, audio_silent_buffer, 2);
		    loadup_dma(acl, 0);

		    audio_channel[i].client = NULL;
""", """		if (acl)	
		{
		    fill_buffer(acl, audio_silent_buffer, acl->stereo ? 4 : 2);
		    loadup_dma(acl, 0);

		    audio_channel[i].client = NULL;
		    if (pr)
			pr->client = NULL;	/* still reserved: partner stays set */
""", what="audiointr detach")
c = sub(c, """		} else if (audio_channel[i].pending == 0)
		{
		    AMIGA->intena = AINTCLR | audio_channel[i].dmadone;
		    AMIGA->dmacon = DMACLR | audio_channel[i].enable;
		}""", """		} else if (audio_channel[i].pending == 0)
		{
		    AMIGA->intena = AINTCLR | audio_channel[i].dmadone;
		    AMIGA->dmacon = DMACLR | audio_channel[i].enable;
		    if (pr)
			stop_channel(pr);
		    audio_channel[i].partner = 0;
		}""", what="audiointr idle")
c = sub(c, """		    audio_channel[i].pending = 0;
		    AMIGA->intena = AINTCLR | audio_channel[i].dmadone;
		    AMIGA->dmacon = DMACLR | audio_channel[i].enable;
		}""", """		    audio_channel[i].pending = 0;
		    AMIGA->intena = AINTCLR | audio_channel[i].dmadone;
		    AMIGA->dmacon = DMACLR | audio_channel[i].enable;
		    if (pr)
			stop_channel(pr);
		    audio_channel[i].partner = 0;
		}""", what="audiointr pending<0")

# ---- audiointr: a partly copied message must advance (stock defect) ---------
c = sub(c, """		    size = fill_buffer(acl, (signed char *)acl->mp->b_rptr, blen);

		    if (size == blen)
		    {
			freemsg(acl->mp);
			acl->mp = NULL;
		    }""", """		    size = fill_buffer(acl, (signed char *)acl->mp->b_rptr, blen);

		    if (size == blen)
		    {
			freemsg(acl->mp);
			acl->mp = NULL;
		    }
		    else
			acl->mp->b_rptr += size;	/* the rest next time, not all of it again */""",
        what="audiointr partial mp")
c = sub(c, """			if (size != blen)
			{
			    acl->mp = mp;""", """			if (size != blen)
			{
			    mp->b_rptr += size;
			    acl->mp = mp;""", what="audiointr partial getq")

# ---- postaudio .. loadup_dma: rewritten ------------------------------------
c = between(c, "int\npostaudio(queue_t *q, mblk_t *mp)", "static void\nrelease_channel_buffer(", r'''int
postaudio(queue_t *q, mblk_t *mp)
{
    struct audio_client *acl = (struct audio_client *)q->q_ptr;
    struct audio_channel *ach[2];
    int size;

    if (grab_channel(acl))
	return 1;			/* <XXX> Could return actual error code */

    ach[0] = &audio_channel[acl->ports[0]];
    ach[0]->client = acl;

    if (acl->stereo)
    {
	ach[1] = &audio_channel[acl->ports[1]];
	ach[1]->client = acl;
	/* Not (still) running as this pair, e.g. one half was idle or the
	** left one was last a mono channel: restart both from scratch. */
	if (ach[0]->partner != acl->ports[1] + 1 || ach[0]->pending != ach[1]->pending)
	{
	    stop_channel(ach[0]);
	    stop_channel(ach[1]);
	    ach[0]->partner = acl->ports[1] + 1;
	}
    }
    else if (ach[0]->partner)
    {
	/* a left channel whose stereo tail is still playing: drop the tail */
	stop_channel(&audio_channel[ach[0]->partner - 1]);
	stop_channel(ach[0]);
	ach[0]->partner = 0;
    }

    switch (ach[0]->pending)
    {
    case 0:				/* No sound currently playing. */
	/*
	** Handfeed first byte and load up the audio hardware to grab
	** dma registers at next scan line.  This is the only way to
	** garrantee that we will play the first dma'd audio sound to
	** a channel.
	*/

	size = fill_buffer(acl, (signed char *)mp->b_rptr, blklen(mp));
	mp->b_rptr += size;
	handfeed(acl);
	break;

    case 1:				/* One sound is playing. */
	/*
	** Set the dma registers to play this audio next time around.
	*/
	size = fill_buffer(acl, (signed char *)mp->b_rptr, blklen(mp));
	mp->b_rptr += size;
	loadup_dma(acl, 0);
	break;

    default:				/* Both buffers are full. */
	/*
	** The next filled buffer will be loaded at interupt time when the
	** currently playing buffer has finished.
	*/
	break;
    }

    return  !(mp->b_rptr == mp->b_wptr);
}

/* silence a channel and forget its buffers (stereo pair (re)start, tails) */
static void
stop_channel(struct audio_channel *ach)
{
    AMIGA->intena = AINTCLR | ach->dmadone;
    AMIGA->dmacon = DMACLR | ach->enable;
    AMIGA->intreq = AINTCLR | ach->dmadone;
    ach->pending = 0;
    ach->used = 0;
    ach->nbytes[0] = ach->nbytes[1] = ach->nbytes[2] = 0;
}

static int
fill_buffer(struct audio_client *acl, signed char *data, int len)
{
    int size;
    struct audio_channel *ach[2];

    ach[0] = &audio_channel[acl->ports[0]];

    if (acl->stereo)
    {
	/* interleaved L,R: both channels take the same number of frames */
	signed char *l, *r;
	int k;

	ach[1] = &audio_channel[acl->ports[1]];
	size = min(len / 2, ach[0]->size - ach[0]->nbytes[ach[0]->used]);
	size = min(size, ach[1]->size - ach[1]->nbytes[ach[1]->used]);
	l = ach[0]->buffer[ach[0]->used] + ach[0]->nbytes[ach[0]->used];
	r = ach[1]->buffer[ach[1]->used] + ach[1]->nbytes[ach[1]->used];
	for (k = 0; k < size; ++k)
	{
	    l[k] = data[2 * k];
	    r[k] = data[2 * k + 1];
	}
	ach[0]->nbytes[ach[0]->used] += size;
	ach[1]->nbytes[ach[1]->used] += size;
	/* a trailing odd byte is no frame: count it as taken */
	return size == len / 2 ? len : 2 * size;
    }

    size = min(len, ach[0]->size - ach[0]->nbytes[ach[0]->used]);

    bcopy(data, ach[0]->buffer[ach[0]->used] + ach[0]->nbytes[ach[0]->used], size);
    ach[0]->nbytes[ach[0]->used] += size;

    return size;
}

/* free for a new client: nobody's, and not the right half of a live pair */
static int
chan_free(int j)
{
    int k;

    if (audio_channel[j].client != NULL)
	return 0;
    for (k = 0; k < MAXCHANNELS; ++k)
	if (audio_channel[k].partner == j + 1)
	    return 0;
    return 1;
}

static int
grab_channel(struct audio_client *acl)
{
    int i;

    if (!(acl->flags&AUD_CATTACHED))	/* This client has no dedicated port */
    {
	if (acl->stereo)
	{
	    /* Paula wiring: channels 0 and 3 left, 1 and 2 right */
	    int l = acl->ports[0], r = acl->ports[1];

	    /* Our own pair, still playing out its tail after the queue ran dry
	    ** (audiointr detached us, as it does a mono client): take it back
	    ** as it is, so playback continues without a restart -- as a mono
	    ** client simply keeps its old port. */
	    if (!(audio_channel[l].client == NULL && audio_channel[r].client == NULL &&
		  audio_channel[l].partner == r + 1))
	    {
		l = chan_free(0) ? 0 : chan_free(3) ? 3 : -1;
		r = chan_free(1) ? 1 : chan_free(2) ? 2 : -1;
		if (l < 0 || r < 0)
		    return EBUSY;
	    }
	    acl->ports[0] = l;
	    acl->ports[1] = r;
	}
	/* First check if old port is available */
	else if (!chan_free(acl->ports[0]))
	{
	    for (i=0; i < MAXCHANNELS; ++i)
	    {
		if (chan_free(i)) /* is channel allocated? */
		{
		    acl->ports[0] = i;	/* no, this is the port we will use */
		    break;
		}
	    }

	    if (i >= MAXCHANNELS)
	    {
		return EBUSY;		/* not enough channels free */
	    }
	}

	acl->flags |= AUD_CATTACHED;
    }

    if (allocate_channel_buffers(acl))
    {
	acl->flags &= ~AUD_CATTACHED;
	return ENOMEM;
    }

    return 0;
}

static int
allocate_one(struct audio_channel *ach)
{
    if (ach->size == 0)
    {
	if (!(ach->buffer[0] = AllocMem(AUDIO_BUFFER_SIZE, MEMF_CHIP)))
	    return ENOMEM;
	if (!(ach->buffer[1] = AllocMem(AUDIO_BUFFER_SIZE, MEMF_CHIP)))
	{
	    FreeMem(ach->buffer[0], AUDIO_BUFFER_SIZE);
	    return ENOMEM;
	}
	if (!(ach->buffer[2] = AllocMem(AUDIO_BUFFER_SIZE, MEMF_CHIP)))
	{
	    FreeMem(ach->buffer[0], AUDIO_BUFFER_SIZE);
	    FreeMem(ach->buffer[1], AUDIO_BUFFER_SIZE);
	    return ENOMEM;
	}

	ach->size = AUDIO_BUFFER_SIZE;
	ach->nbytes[0] = ach->nbytes[1] = ach->nbytes[2] = 0;
	ach->pending = 0;
    }

    return 0;
}

static int
allocate_channel_buffers(struct audio_client *acl)
{
    if (allocate_one(&audio_channel[acl->ports[0]]))
	return ENOMEM;
    if (acl->stereo && allocate_one(&audio_channel[acl->ports[1]]))
	return ENOMEM;
    return 0;
}

static void
handfeed(struct audio_client *acl)
{
    struct audio_channel *ach[2];
    int done, dma;

    ach[0] = &audio_channel[acl->ports[0]];
    ach[1] = acl->stereo ? &audio_channel[acl->ports[1]] : ach[0];
    done = ach[0]->dmadone | ach[1]->dmadone;
    dma = ach[0]->enable | ach[1]->enable;

    AMIGA->intena = AINTCLR | done;	/* disable interupts */
    AMIGA->dmacon = DMACLR | dma;	/* disable dma for now */
    AMIGA->intreq = AINTCLR | done;	/* clear interupt */

    loadup_dma(acl, AU_L_NOAUDIO);
    ach[0]->audio->data = 0;		/* We feed the audio hardware a silent yell */
    ach[1]->audio->data = 0;		/* until we can post the actual sound in */
					/* the interupt routine */

    while ((AMIGA->intreqr & done) != done)
	;				/* busy wait for hand fed word(s) */

    AMIGA->dmacon = DMASET | dma;	/* enable dma: both halves on one write */
    AMIGA->intreq = AINTCLR | done;	/* clear interupt */
    AMIGA->intena = AINTSET | ach[0]->dmadone;	/* only the left one interrupts */

    ach[0]->audio->loud = acl->volume;
    ach[0]->audio->period = acl->period;
    ach[1]->audio->loud = acl->volume;
    ach[1]->audio->period = acl->period;
}

static void
loadup_one(struct audio_client *acl, struct audio_channel *ach, int flags)
{
    if (ach->nbytes[ach->used] == 0)
    {
	printf("WARNING loadup_dma called with no buffer ready\n");
	return;
    }

    ach->audio->buf = ach->buffer[ach->used];
    ach->audio->size = ach->nbytes[ach->used] / 2;

    if (ach->used == 2)
	ach->used = 0;
    else
	++ach->used;

    if (flags&AU_L_NOAUDIO)
    {
	ach->audio->period = 1;
	ach->audio->loud = 0;
    }
    else
    {
	ach->audio->period = acl->period;
	ach->audio->loud = acl->volume;
    }

    ++ach->pending;
}

static void
loadup_dma(struct audio_client *acl, int flags)
{
    loadup_one(acl, &audio_channel[acl->ports[0]], flags);
    if (acl->stereo)
	loadup_one(acl, &audio_channel[acl->ports[1]], flags);
}

''', "postaudio..loadup_dma")

open(cpath, "w", encoding="latin-1").write(c)
open(hpath, "w", encoding="latin-1").write(h)
print("      [ok] AUDIO_STEREO: locked L/R pair (0|3 + 1|2), one interrupt, one dmacon write")
print("      [ok] audiointr: partly copied messages advance (no replay)")
print("      [ok] ioctl: AUDIO_VOLUME/AUDIO_PERIOD acknowledged once, with ioc_error/ioc_rval set")
