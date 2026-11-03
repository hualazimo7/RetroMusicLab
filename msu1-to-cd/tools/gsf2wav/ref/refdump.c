/* refdump.c — GSF（GBA，PSF 版本 0x22）的「黄金参照」导出器
 *             **只在开发机用，不进发布包**
 *
 * 这份代码是 kode54 `audacious-xsf/plugin.cc` 里 GSF 路径的**逐行移植**：
 *
 *   gsf_loader              ← 原样
 *   struct gsf_running_state / _gsf_postAudioBuffer  ← 原样
 *   xsf_open() 里 `psf_version == 0x22` 那一支       ← 原样
 *   xsf_render() 里 `case 0x22`                      ← 原样
 *   play() 里的时长/淡出算法                          ← 原样
 *   parse_time_crap                                   ← 原样（含 1000.0f 那套）
 *
 * 唯一改掉的是 audacious 的 `write_audio()`，换成写裸 PCM。
 *
 * 为什么需要它
 * ------------
 * 判据必须是「逐样本最大差 == 0」。如果参照物就是我自己写的 `gsf2wav.c`，
 * 那只是拿自己验自己。这里拿的是**上游作者的原始实现**当尺子 —— 容器解析、
 * ROM 对齐、帧数换算、淡出斜坡、`runFrame` 的推进粒度，只要我抄错一处，
 * 样本就会差。
 *
 * 采样率
 * ------
 * `plugin.cc` 的 `get_srate(0x22)` 是 **32768**，这是默认值。
 * `gsf2wav` 出厂用 44100（主流水线要求），所以比对时要：
 *
 *   refdump in.gsf out.pcm              → 32768（上游原值）
 *   refdump in.gsf out.pcm --rate 44100 → 和 gsf2wav 的出厂设置对齐
 *
 * 两条都验：32768 那条证明「接线抄对了」，44100 那条证明「出厂设置也对」。
 *
 * 用法
 * ----
 *   refdump <input.gsf|.minigsf> <output.pcm> [--rate <hz>]
 *
 * 退出码同 gsf2wav。
 */
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <zlib.h>

#include <psflib.h>

#include <mgba/core/core.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/log.h>
#include <mgba-util/vfs.h>

#define OUT_CH          2
#define MIN_BUFFER_SIZE 576     /* ⚠ 必须和 plugin.cc 一致（plugin.cc:56） */
#define UPSTREAM_RATE   32768   /* get_srate(0x22) */

enum {
    EXIT_OK = 0,
    EXIT_USAGE = 1,
    EXIT_PARSE = 2,
    EXIT_EMPTY = 3,
    EXIT_NOMEM = 4
};

/* ------------------------------------------------------------------ */
/*  上游的 parse_time_crap                                             */
/* ------------------------------------------------------------------ */

#define BORK_TIME 0xC0FFEE

static uint32_t get_le32(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8)
         | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

static unsigned long parse_time_crap(const char *input)
{
    unsigned long value = 0;
    unsigned long multiplier = 1000;
    const char *ptr = input;
    unsigned long colon_count = 0;

    while (*ptr && ((*ptr >= '0' && *ptr <= '9') || *ptr == ':')) {
        colon_count += *ptr == ':';
        ++ptr;
    }
    if (colon_count > 2) return BORK_TIME;
    if (*ptr && *ptr != '.' && *ptr != ',') return BORK_TIME;
    if (*ptr) ++ptr;
    while (*ptr && *ptr >= '0' && *ptr <= '9') ++ptr;
    if (*ptr) return BORK_TIME;

    ptr = strrchr(input, ':');
    if (!ptr) ptr = input;
    for (;;) {
        char *end;
        if (ptr != input) ++ptr;
        if (multiplier == 1000) {
            double temp = strtod(ptr, &end);
            if (temp >= 60.0) return BORK_TIME;
            value = (long)(temp * 1000.0f);
        } else {
            unsigned long temp = strtoul(ptr, &end, 10);
            if (temp >= 60 && multiplier < 3600000) return BORK_TIME;
            value += temp * multiplier;
        }
        if (ptr == input) break;
        ptr -= 2;
        while (ptr > input && *ptr != ':') --ptr;
        multiplier *= 60;
    }

    return value;
}

/* ------------------------------------------------------------------ */
/*  上游的 psf_info_meta（只留 length / fade）                          */
/* ------------------------------------------------------------------ */

struct info_state {
    int tag_song_ms;
    int tag_fade_ms;
    char title[256];
    char game[256];
    char length_s[64];
    char fade_s[64];
};

static int info_meta(void *context, const char *name, const char *value)
{
    struct info_state *st = (struct info_state *)context;

    if (!strcasecmp(name, "length")) {
        int temp = (int)parse_time_crap(value);
        if (temp != BORK_TIME) {
            st->tag_song_ms = temp;
            snprintf(st->length_s, sizeof(st->length_s), "%s", value);
        }
    } else if (!strcasecmp(name, "fade")) {
        int temp = (int)parse_time_crap(value);
        if (temp != BORK_TIME) {
            st->tag_fade_ms = temp;
            snprintf(st->fade_s, sizeof(st->fade_s), "%s", value);
        }
    } else if (!strcasecmp(name, "title")) {
        snprintf(st->title, sizeof(st->title), "%s", value);
    } else if (!strcasecmp(name, "game")) {
        snprintf(st->game, sizeof(st->game), "%s", value);
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/*  上游的 gsf_loader_state / gsf_loader                               */
/* ------------------------------------------------------------------ */

struct gsf_loader_state {
    unsigned char *data;
    size_t data_size;
    unsigned entry;
    int entry_set;
};

static int gsf_loader(void *context, const uint8_t *exe, size_t exe_size,
                      const uint8_t *reserved, size_t reserved_size)
{
    if (exe_size < 12) return -1;

    struct gsf_loader_state *state = (struct gsf_loader_state *)context;

    unsigned char *iptr;
    size_t isize;
    unsigned char *xptr;
    unsigned xentry = get_le32(exe + 0);
    unsigned xsize = get_le32(exe + 8);
    unsigned xofs = get_le32(exe + 4) & 0x1ffffff;

    (void)reserved;
    (void)reserved_size;

    if (xsize < exe_size - 12) return -1;
    if (!state->entry_set) {
        state->entry = xentry;
        state->entry_set = 1;
    }
    {
        iptr = state->data;
        isize = state->data_size;
        state->data = 0;
        state->data_size = 0;
    }
    if (!iptr) {
        size_t rsize = xofs + xsize;
        {
            rsize -= 1;
            rsize |= rsize >> 1;
            rsize |= rsize >> 2;
            rsize |= rsize >> 4;
            rsize |= rsize >> 8;
            rsize |= rsize >> 16;
            rsize += 1;
        }
        iptr = (unsigned char *)malloc(rsize + 10);
        if (!iptr) return -1;
        memset(iptr, 0, rsize + 10);
        isize = rsize;
    } else if (isize < xofs + xsize) {
        size_t rsize = xofs + xsize;
        {
            rsize -= 1;
            rsize |= rsize >> 1;
            rsize |= rsize >> 2;
            rsize |= rsize >> 4;
            rsize |= rsize >> 8;
            rsize |= rsize >> 16;
            rsize += 1;
        }
        xptr = (unsigned char *)realloc(iptr, xofs + rsize + 10);
        if (!xptr) {
            free(iptr);
            return -1;
        }
        iptr = xptr;
        isize = rsize;
    }
    memcpy(iptr + xofs, exe + 12, xsize);
    {
        state->data = iptr;
        state->data_size = isize;
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/*  上游的 gsf_running_state / _gsf_postAudioBuffer                    */
/* ------------------------------------------------------------------ */

struct gsf_running_state {
    struct mAVStream stream;
    void *rom;
    int16_t samples[MIN_BUFFER_SIZE * 2];
    int buffered;
};

static void _gsf_postAudioBuffer(struct mAVStream *stream, blip_t *left,
                                 blip_t *right)
{
    struct gsf_running_state *state = (struct gsf_running_state *)stream;
    blip_read_samples(left, state->samples, MIN_BUFFER_SIZE, true);
    blip_read_samples(right, state->samples + 1, MIN_BUFFER_SIZE, true);
    state->buffered = MIN_BUFFER_SIZE;
}

/* ---- psflib 的文件回调 ---- */
static void *f_open(void *ctx, const char *path) { (void)ctx; return (void *)fopen(path, "rb"); }
static size_t f_read(void *buf, size_t size, size_t count, void *h) { return fread(buf, size, count, (FILE *)h); }
static int f_seek(void *h, int64_t ofs, int whence) { return fseek((FILE *)h, (long)ofs, whence); }
static int f_close(void *h) { return fclose((FILE *)h); }
static long f_tell(void *h) { return ftell((FILE *)h); }

static const psf_file_callbacks g_callbacks = {
    "\\/", NULL, f_open, f_read, f_seek, f_close, f_tell
};

/*: 关掉 mGBA 的日志。和 gsf2wav 一样 —— 只影响日志，不影响音频。
 *  必须在 `mCoreInitConfig()` 之后装。 */
static void noop_log(struct mLogger *logger, int category,
                     enum mLogLevel level, const char *format, va_list args)
{
    (void)logger; (void)category; (void)level; (void)format; (void)args;
}

static struct mLogger g_noop_logger = { noop_log, NULL };

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    const char *in_path = NULL;
    const char *out_path = NULL;
    uint32_t sample_rate = UPSTREAM_RATE;
    int i;

    struct gsf_loader_state state;
    struct info_state info;
    struct gsf_running_state *rstate = NULL;
    struct mCore *core = NULL;
    struct VFile *rom = NULL;
    FILE *out = NULL;
    short buffer[MIN_BUFFER_SIZE * 2];
    int max_buffer_samples = MIN_BUFFER_SIZE;
    int stream_samples_amount, fade_samples;
    int current_sample_pos = 0;
    long written = 0;
    int rval;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--rate")) {
            if (i + 1 >= argc) {
                fprintf(stderr, "用法: refdump <in.gsf> <out.pcm> [--rate <hz>]\n");
                return EXIT_USAGE;
            }
            sample_rate = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (!in_path) {
            in_path = argv[i];
        } else if (!out_path) {
            out_path = argv[i];
        }
    }
    if (!in_path || !out_path) {
        fprintf(stderr, "用法: refdump <in.gsf> <out.pcm> [--rate <hz>]\n");
        return EXIT_USAGE;
    }

    memset(&state, 0, sizeof(state));
    memset(&info, 0, sizeof(info));

    /* ---- xsf_open(): 先读 meta 拿 length/fade ---- */
    rval = psf_load(in_path, &g_callbacks, 0, 0, 0, info_meta, &info, 0, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr, "ERR 读 meta 失败 (%d)\n", rval);
        return EXIT_PARSE;
    }

    /* ---- xsf_open(): psf_version == 0x22 那一支 ---- */
    if (psf_load(in_path, &g_callbacks, 0x22, gsf_loader, &state,
                 0, 0, 1, NULL, NULL) <= 0) {
        fprintf(stderr, "ERR psf_load 失败\n");
        return EXIT_PARSE;
    }

    rom = VFileFromConstMemory(state.data, state.data_size);
    if (!rom) {
        fprintf(stderr, "ERR VFileFromConstMemory 失败\n");
        return EXIT_NOMEM;
    }

    core = mCoreFindVF(rom);
    if (!core) {
        fprintf(stderr, "ERR mCoreFindVF 失败\n");
        return EXIT_PARSE;
    }

    rstate = (struct gsf_running_state *)calloc(1, sizeof(*rstate));
    if (!rstate) return EXIT_NOMEM;

    rstate->rom = state.data;
    rstate->stream.postAudioBuffer = _gsf_postAudioBuffer;

    core->init(core);
    core->setAVStream(core, &rstate->stream);
    mCoreInitConfig(core, NULL);
    mLogSetDefaultLogger(&g_noop_logger);   /* ⚠ 必须在 mCoreInitConfig 之后 */

    core->setAudioBufferSize(core, MIN_BUFFER_SIZE);

    blip_set_rates(core->getAudioChannel(core, 0), core->frequency(core), sample_rate);
    blip_set_rates(core->getAudioChannel(core, 1), core->frequency(core), sample_rate);

    {
        struct mCoreOptions opts = { 0 };
        opts.skipBios = true;
        opts.useBios = false;
        opts.sampleRate = sample_rate;
        opts.volume = 0x100;
        mCoreConfigLoadDefaults(&core->config, &opts);
    }

    core->loadROM(core, rom);
    core->reset(core);

    /* ---- play() 的时长/淡出算法，原样 ---- */
    /* 没有 length 标签时的兜底：plugin.cc 用 settings.fade_delay(170) +
       settings.fade_length(默认 10)，即 180 秒。这里写死同一数值。 */
    {
        long long total_ms = info.tag_song_ms
            ? (long long)(info.tag_song_ms + info.tag_fade_ms)
            : (long long)(170.0 + 10.0) * 1000;
        long long fade_ms = info.tag_song_ms
            ? (long long)info.tag_fade_ms
            : (long long)(10.0 * 1000);
        stream_samples_amount = (int)(total_ms * sample_rate / 1000LL);
        fade_samples = (int)(fade_ms * sample_rate / 1000LL);
    }

    out = fopen(out_path, "wb");
    if (!out) {
        fprintf(stderr, "ERR 无法写入 %s\n", out_path);
        return EXIT_USAGE;
    }

    /* ---- play() 主循环，原样（去掉 check_stop / check_seek） ---- */
    while (1) {
        int toget = max_buffer_samples;

        if (current_sample_pos >= stream_samples_amount)
            break;
        if (current_sample_pos + toget > stream_samples_amount)
            toget = stream_samples_amount - current_sample_pos;

        /* xsf_render(): case 0x22 */
        {
            unsigned long frames_to_render = (unsigned long)toget;
            int16_t *buf = buffer;

            do {
                unsigned long frames_rendered = (unsigned long)rstate->buffered;

                if (frames_rendered >= frames_to_render) {
                    if (buf) memcpy(buf, rstate->samples, frames_to_render * 4);
                    frames_rendered -= frames_to_render;
                    /* 上游这里写的是 memcpy；源在前、目标在后且区间重叠，
                       memcpy 属未定义行为。换成 memmove 语义相同且不靠运气。 */
                    memmove(rstate->samples,
                            rstate->samples + frames_to_render * 2,
                            frames_rendered * 4);
                    frames_to_render = 0;
                } else {
                    if (buf) {
                        memcpy(buf, rstate->samples, frames_rendered * 4);
                        buf = (int16_t *)(((uint8_t *)buf) + frames_rendered * 4);
                    }
                    frames_to_render -= frames_rendered;
                    frames_rendered = 0;
                }
                rstate->buffered = (int)frames_rendered;

                if (frames_to_render) {
                    while (!rstate->buffered)
                        core->runFrame(core);
                }
            } while (frames_to_render);
        }

        if (fade_samples > 0) {
            int samples_into_fade =
                    current_sample_pos - (stream_samples_amount - fade_samples);
            if (samples_into_fade + toget > 0) {
                for (int j = 0; j < toget; j++, samples_into_fade++) {
                    if (samples_into_fade > 0) {
                        double fadedness =
                                (double)(fade_samples - samples_into_fade) / fade_samples;
                        for (int k = 0; k < 2; k++)
                            buffer[j * 2 + k] *= fadedness;
                    }
                }
            }
        }

        fwrite(buffer, sizeof(short) * OUT_CH, (size_t)toget, out);
        written += toget;
        current_sample_pos += toget;
    }

    fclose(out);

    if (written == 0) {
        fprintf(stderr, "ERR 渲染出 0 个样本\n");
        return EXIT_EMPTY;
    }

    if (getenv("REFDUMP_VERBOSE"))
        fprintf(stderr, "OK %ld 帧 %.3f 秒 @%u\n",
                written, (double)written / sample_rate, sample_rate);

    return EXIT_OK;
}
