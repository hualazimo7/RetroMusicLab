/* gsf2wav.c — GSF / minigsf（Nintendo GBA，PSF 版本 0x22）→ WAV
 *
 * 核：mGBA 0.10.5 的最小核（`MINIMAL_CORE=2`，只留 GBA）。
 * 接线照 kode54 `audacious-xsf/plugin.cc` 的 `psf_version == 0x22` 分支。
 *
 * ---------------------------------------------------------------------
 *  这个核和 usf2wav / vio2wav / ssf2wav 不一样的三处
 * ---------------------------------------------------------------------
 *
 *  1. **原生采样率是 32768，不是 44100。**
 *     `plugin.cc` 里 `get_srate(0x22)` 返回 32768，`blip_set_rates(...)` 也
 *     按 32768 出样。但主流水线（`msu1_to_cd.py`）写 WAV 时一律标
 *     `writearamerate(44100)`，所以这里必须**自己重采样到 44100**，
 *     否则成品会快 1.346 倍、音高也跟着飘。
 *
 *     重采样不另写算法 —— 直接让 mGBA 自带的 blip_buf 出 44100
 *     （`blip_set_rates` 的第二个参数就是输出率，它本来就是带限合成器）。
 *     于是 32768 → 44100 只差**一个常数**，没有第二套 DSP。
 *
 *     `--rate 32768` 可以把这一个常数改回上游值，逐样本比对时用得上
 *     （见 `ref/refdump.c`）。
 *
 *  2. **按「帧」推进，不是按周期。**
 *     `plugin.cc` 的 0x22 分支用的是 `core->runFrame(core)` —— 一帧是
 *     280896 个周期（约 16.74ms）。`runLoop` 会按周期切，切点不同，
 *     采样点就不同。必须照抄 `runFrame`。
 *
 *  3. **每块 576 帧（`MIN_BUFFER_SIZE`）。**
 *     和 ssf2wav 同样的坑：这个值不是随便挑的，它是 `setAudioBufferSize`
 *     和 `blip_read_samples` 的共同参数。改了，每次回调吐的样本数就变，
 *     逐样本比对立刻对不上。
 *
 * ---------------------------------------------------------------------
 *  容器（照 `gsf_loader`）
 * ---------------------------------------------------------------------
 *
 *  exe 段：`u32 entry + u32 xofs + u32 xsize + 数据`。
 *  ROM 尺寸要**向上取整到 2 的幂再加 10 字节** —— mGBA 用 `rom_mask`
 *  位与寻址，尺寸不对齐会读越界或者读错地址。
 *  多段（主文件 + `.gsflib`）由 psflib 依次回调，`gsf_loader` 把它们
 *  合并进同一块内存。
 *
 *  退出码：0 成功 / 1 用法错 / 2 解析失败 / 3 没渲染出样本 / 4 内存不足
 */

#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <psflib.h>

#include <mgba/core/core.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/log.h>
#include <mgba-util/vfs.h>

/*: 上游 `plugin.cc:56` 的 `MIN_BUFFER_SIZE`。见文件头第 3 条。 */
#define BUF_FRAMES   576

/*: GSF 的原生输出率。只有 `--rate` 不传时才会用到它做参照。 */
#define NATIVE_RATE  32768

/*: 主流水线要的率。 */
#define PIPELINE_RATE 44100

#define OUT_CH       2
#define OUT_BITS     16
#define DEFAULT_SECS 180.0

enum {
    EXIT_OK = 0,
    EXIT_USAGE = 1,
    EXIT_PARSE = 2,
    EXIT_EMPTY = 3,
    EXIT_NOMEM = 4
};

/* ------------------------------------------------------------------ */
/*  把 mGBA 的日志关掉                                                 */
/* ------------------------------------------------------------------ */
/*  mGBA 默认日志器会把 `GBA BIOS: SWI: 0B …` / `GBA DMA: Starting …`
 *  这类东西全打到 stderr。一首 30 秒的曲子能刷出 200 KB —— 既拖慢速度，
 *  也让 `_last_err()` 挑不出真正的错误行。
 *
 *  这里换成一个什么都不做的日志器。**只影响日志，不影响音频**。
 *  注意时机：必须在 `mCoreInitConfig()` 之后装，否则会被它覆盖掉。 */
static void noop_log(struct mLogger *logger, int category,
                     enum mLogLevel level, const char *format, va_list args)
{
    (void)logger; (void)category; (void)level; (void)format; (void)args;
}

static struct mLogger g_noop_logger = { noop_log, NULL };

/* ------------------------------------------------------------------ */
/*  小工具                                                             */
/* ------------------------------------------------------------------ */

static uint32_t get_le32(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8)
         | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

static void wr16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

static void wr32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static int write_wav_header(FILE *f, uint32_t data_bytes,
                            uint32_t rate, uint16_t channels, uint16_t bits)
{
    uint8_t h[44];
    uint32_t byte_rate = rate * channels * bits / 8;
    uint16_t block_align = (uint16_t)(channels * bits / 8);

    memcpy(h, "RIFF", 4);
    wr32le(h + 4, 36 + data_bytes);
    memcpy(h + 8, "WAVE", 4);
    memcpy(h + 12, "fmt ", 4);
    wr32le(h + 16, 16);
    wr16le(h + 20, 1);                 /* PCM */
    wr16le(h + 22, channels);
    wr32le(h + 24, rate);
    wr32le(h + 28, byte_rate);
    wr16le(h + 32, block_align);
    wr16le(h + 34, bits);
    memcpy(h + 36, "data", 4);
    wr32le(h + 40, data_bytes);
    return fwrite(h, 1, 44, f) == 44 ? 0 : -1;
}

/*: `1:18.6` / `83.5` / `1:23` → 毫秒。非法返回 -1。
 *
 * 抄自 lazyusf2 的 `test/dumpresampled.c`，和 usf2wav / vio2wav / ssf2wav
 * 是同一份。⚠ 别凭印象重写成「分:秒.百分秒」—— 上游是用 `strtod` 读
 * 最后一节，`18.6` 就是 18600 毫秒。 */
static long parse_time_ms(const char *s)
{
    long value = 0;
    long multiplier = 1000;
    const char *ptr = s;
    int colons = 0;

    if (!s || !*s) return -1;

    while (*ptr && ((*ptr >= '0' && *ptr <= '9') || *ptr == ':')) {
        if (*ptr == ':') colons++;
        ++ptr;
    }
    if (colons > 2) return -1;
    if (*ptr && *ptr != '.' && *ptr != ',') return -1;
    if (*ptr) ++ptr;
    while (*ptr && *ptr >= '0' && *ptr <= '9') ++ptr;
    if (*ptr) return -1;

    ptr = strrchr(s, ':');
    if (!ptr) ptr = s;
    for (;;) {
        char *end = NULL;
        if (ptr != s) ++ptr;
        if (multiplier == 1000) {
            double temp = strtod(ptr, &end);
            if (end == ptr || temp < 0.0 || temp >= 60.0) return -1;
            value = (long)(temp * 1000.0);
        } else {
            unsigned long temp = strtoul(ptr, &end, 10);
            if (end == ptr) return -1;
            if (temp >= 60 && multiplier < 3600000) return -1;
            value += (long)temp * multiplier;
        }
        if (ptr == s) break;
        ptr -= 2;
        while (ptr > s && *ptr != ':') --ptr;
        multiplier *= 60;
    }
    return value;
}

/* ------------------------------------------------------------------ */
/*  GSF 装载                                                            */
/* ------------------------------------------------------------------ */
/*  逐行照抄 `plugin.cc` 的 `gsf_loader`。                              */
/*                                                                     */
/*  exe 段布局：`u32 entry + u32 xofs + u32 xsize + 数据`。             */
/*  xofs 要 `& 0x1ffffff` 掩码（25 位，32MB 寻址空间）。                */
/*  多段合并：起点更靠前就前移补 0，终点更靠后就扩容补 0。              */
/*  ROM 尺寸向上取整到 2 的幂，**再多给 10 字节** —— mGBA 的             */
/*  `GBACoreLoadROM` 会往后读一点点（存档区探测），上游就是这么给的。   */
/* ------------------------------------------------------------------ */

typedef struct {
    uint8_t *data;
    size_t   data_size;
    uint32_t entry;
    int      entry_set;
    int      n_section;

    char title[256];
    char game[256];
    char length_s[64];
    char fade_s[64];
} loadctx;

static size_t round_up_pow2(size_t n)
{
    n -= 1;
    n |= n >> 1;
    n |= n >> 2;
    n |= n >> 4;
    n |= n >> 8;
    n |= n >> 16;
    n += 1;
    return n;
}

static int gsf_load(void *context, const uint8_t *exe, size_t exe_size,
                    const uint8_t *reserved, size_t reserved_size)
{
    loadctx *c = (loadctx *)context;
    unsigned char *iptr;
    size_t isize;
    unsigned char *xptr;
    unsigned xentry, xsize, xofs;

    (void)reserved;
    (void)reserved_size;

    if (exe_size < 12) return -1;

    xentry = get_le32(exe + 0);
    xofs   = get_le32(exe + 4) & 0x1ffffffu;
    xsize  = get_le32(exe + 8);
    /* ⚠ 上游这一行看着像「长度必须刚好等于声明值」，实际是防溢出：
       声明的 xsize 不能超过段里真实带的数据量。照抄，别「修正」。 */
    if (xsize < exe_size - 12) return -1;

    if (!c->entry_set) {
        c->entry = xentry;
        c->entry_set = 1;
    }

    iptr = c->data;
    isize = c->data_size;
    c->data = NULL;
    c->data_size = 0;

    if (!iptr) {
        size_t rsize = round_up_pow2((size_t)xofs + xsize);
        iptr = (unsigned char *)malloc(rsize + 10);
        if (!iptr) return -1;
        memset(iptr, 0, rsize + 10);
        isize = rsize;
    } else if (isize < (size_t)xofs + xsize) {
        size_t rsize = round_up_pow2((size_t)xofs + xsize);
        xptr = (unsigned char *)realloc(iptr, (size_t)xofs + rsize + 10);
        if (!xptr) {
            free(iptr);
            return -1;
        }
        iptr = xptr;
        isize = rsize;
    }

    memcpy(iptr + xofs, exe + 12, xsize);

    c->data = iptr;
    c->data_size = isize;
    c->n_section++;
    return 0;
}

static int info_cb(void *context, const char *name, const char *value)
{
    loadctx *c = (loadctx *)context;
    if (!strcmp(name, "title"))      snprintf(c->title,    sizeof(c->title),    "%s", value);
    if (!strcmp(name, "game"))       snprintf(c->game,     sizeof(c->game),     "%s", value);
    if (!strcmp(name, "length"))     snprintf(c->length_s, sizeof(c->length_s), "%s", value);
    if (!strcmp(name, "fade"))       snprintf(c->fade_s,   sizeof(c->fade_s),   "%s", value);
    return 0;
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

/* ------------------------------------------------------------------ */
/*  mGBA 的音频回调                                                     */
/* ------------------------------------------------------------------ */
/*  照抄 `plugin.cc` 的 `struct gsf_running_state` +                    */
/*  `_gsf_postAudioBuffer`。                                            */
/*                                                                     */
/*  ⚠ `stream` **必须**是第一个成员 —— 核拿到的是 `struct mAVStream *`，*/
/*  回调里靠指针相等直接强转回外层结构。挪了位置就是读垃圾。            */
/*                                                                     */
/*  `blip_read_samples(..., stereo=1)` 只写偶数下标，所以两次调用        */
/*  分别写 `samples` 和 `samples + 1`，合起来就是交错立体声。           */
/* ------------------------------------------------------------------ */

struct gsf_running_state {
    struct mAVStream stream;
    void *rom;
    int16_t samples[BUF_FRAMES * OUT_CH];
    int buffered;
};

static void gsf_post_audio(struct mAVStream *stream, blip_t *left, blip_t *right)
{
    struct gsf_running_state *st = (struct gsf_running_state *)stream;
    blip_read_samples(left,  st->samples,     BUF_FRAMES, true);
    blip_read_samples(right, st->samples + 1, BUF_FRAMES, true);
    st->buffered = BUF_FRAMES;
}

/*: 从核里取 `n` 帧，照抄 `xsf_render()` 的 `case 0x22`。
 *
 *  上游的循环结构是「先看缓冲区里有没有存货，不够就跑整帧」——
 *  一帧只有 280896/16777216 ≈ 16.74ms，在 44100 下约 738 个样本，
 *  比 576 多；在 32768 下约 548 个，比 576 少。所以**有时候一帧就够、
 *  有时候要两帧**。这个不对称必须靠上游的 while 循环处理，
 *  不能简化成「每次跑固定帧数」。 */
static void gsf_render(struct mCore *core, struct gsf_running_state *st,
                       int16_t *buf, unsigned long frames)
{
    unsigned long frames_to_render = frames;

    do {
        unsigned long frames_rendered = (unsigned long)st->buffered;

        if (frames_rendered >= frames_to_render) {
            if (buf) memcpy(buf, st->samples, frames_to_render * 4);
            frames_rendered -= frames_to_render;
            memmove(st->samples, st->samples + frames_to_render * 2,
                    frames_rendered * 4);
            frames_to_render = 0;
        } else {
            if (buf) {
                memcpy(buf, st->samples, frames_rendered * 4);
                buf = (int16_t *)(((uint8_t *)buf) + frames_rendered * 4);
            }
            frames_to_render -= frames_rendered;
            frames_rendered = 0;
        }
        st->buffered = (int)frames_rendered;

        if (frames_to_render) {
            while (!st->buffered) {
                core->runFrame(core);
            }
        }
    } while (frames_to_render);
}

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

static void usage(void)
{
    fprintf(stderr,
        "用法:\n"
        "  gsf2wav <input.gsf|.minigsf> <output.wav> [秒数] [--no-fade]\n"
        "  gsf2wav --info <input.gsf|.minigsf>\n"
        "\n"
        "  --no-fade     不叠淡出（逐样本比对时必须加）\n"
        "  --info        只打印标签与段数，不渲染\n"
        "  --rate <hz>   输出采样率，默认 44100；给 32768 就是上游原生值\n");
}

int main(int argc, char **argv)
{
    const char *in_path = NULL;
    const char *out_path = NULL;
    double want_secs = 0.0;
    int info_only = 0;
    int no_fade = 0;
    uint32_t out_rate = PIPELINE_RATE;
    int i;

    loadctx c;
    struct mCore *core = NULL;
    struct VFile *rom = NULL;
    struct gsf_running_state *st = NULL;
    int rval;
    FILE *out = NULL;
    int16_t buf[BUF_FRAMES * OUT_CH];
    int64_t frames_left, total_frames = 0;
    uint32_t data_bytes = 0;
    long length_ms, fade_ms;
    int rc = EXIT_OK;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--info")) {
            info_only = 1;
        } else if (!strcmp(argv[i], "--no-fade")) {
            no_fade = 1;
        } else if (!strcmp(argv[i], "--rate")) {
            if (i + 1 >= argc) { usage(); return EXIT_USAGE; }
            out_rate = (uint32_t)strtoul(argv[++i], NULL, 10);
            if (out_rate < 8000 || out_rate > 192000) {
                fprintf(stderr, "ERR --rate 给的值不合理：%u\n", out_rate);
                return EXIT_USAGE;
            }
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage();
            return EXIT_OK;
        } else if (!in_path) {
            in_path = argv[i];
        } else if (!out_path) {
            out_path = argv[i];
        } else {
            want_secs = atof(argv[i]);
        }
    }
    if (!in_path || (!info_only && !out_path)) {
        usage();
        return EXIT_USAGE;
    }
    if (info_only) out_path = NULL;

    memset(&c, 0, sizeof(c));

    /* ---- 1. 先探版本，必须是 0x22（GBA） ---- */
    rval = psf_load(in_path, &g_callbacks, 0, NULL, NULL, info_cb, &c, 0, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr, "ERR 不是有效的 GSF（psf_load 返回 %d）\n", rval);
        return EXIT_PARSE;
    }
    if (rval != 0x22) {
        fprintf(stderr, "ERR 版本字节是 0x%02x，不是 GSF(0x22)\n", rval);
        return EXIT_PARSE;
    }

    /* ---- 2. 解析容器，跟 `.gsflib` 链，合并成一块 ROM ---- */
    rval = psf_load(in_path, &g_callbacks, (uint8_t)rval, gsf_load, &c,
                    NULL, NULL, 1, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr,
                "ERR psf_load 失败 (%d)：不是有效的 GSF，"
                "或者配套的 .gsflib 找不到\n", rval);
        free(c.data);
        return EXIT_PARSE;
    }
    if (!c.data || c.data_size < 0xC0) {
        /* GBA 卡带头 0xC0 字节，短于这个长度连 `GBAIsROM` 都过不去。 */
        fprintf(stderr,
                "ERR ROM 数据为空或过短：GSF 里没有可执行的游戏数据"
                "（文件可能不完整，或者缺少配套的 .gsflib）\n");
        free(c.data);
        return EXIT_PARSE;
    }

    /* ---- 3. 建核 ---- */
    rom = VFileFromConstMemory(c.data, c.data_size);
    if (!rom) {
        fprintf(stderr, "ERR 无法为 ROM 建虚拟文件\n");
        free(c.data);
        return EXIT_NOMEM;
    }

    core = mCoreFindVF(rom);
    if (!core) {
        fprintf(stderr, "ERR mGBA 认不出这个 ROM（GBAIsROM 没过）\n");
        rom->close(rom);
        free(c.data);
        return EXIT_PARSE;
    }

    st = (struct gsf_running_state *)calloc(1, sizeof(*st));
    if (!st) {
        fprintf(stderr, "ERR 内存不足\n");
        rc = EXIT_NOMEM;
        goto done;
    }
    st->rom = c.data;
    st->stream.postAudioBuffer = gsf_post_audio;

    core->init(core);
    core->setAVStream(core, &st->stream);
    mCoreInitConfig(core, NULL);
    mLogSetDefaultLogger(&g_noop_logger);   /* ⚠ 必须在 mCoreInitConfig 之后 */

    /* ⚠ 576，见文件头第 3 条 */
    core->setAudioBufferSize(core, BUF_FRAMES);

    /* ⚠ 第二个参数是**输出率**。上游是 32768，这里默认 44100 让 blip_buf
       顺手把 32768 → 44100 做了（它就是带限合成器，不用另写重采样）。 */
    blip_set_rates(core->getAudioChannel(core, 0), core->frequency(core), out_rate);
    blip_set_rates(core->getAudioChannel(core, 1), core->frequency(core), out_rate);

    {
        struct mCoreOptions opts = { 0 };
        opts.skipBios = true;
        opts.useBios = false;
        opts.sampleRate = out_rate;
        opts.volume = 0x100;
        mCoreConfigLoadDefaults(&core->config, &opts);
    }

    if (!core->loadROM(core, rom)) {
        fprintf(stderr, "ERR mGBA 装载 ROM 失败\n");
        rc = EXIT_PARSE;
        goto done;
    }
    core->reset(core);

    length_ms = parse_time_ms(c.length_s);
    fade_ms   = parse_time_ms(c.fade_s);
    if (fade_ms < 0) fade_ms = 0;

    if (info_only) {
        printf("version=0x22\n");
        printf("machine=GBA\n");
        printf("title=%s\n", c.title);
        printf("game=%s\n", c.game);
        printf("length=%s\n", c.length_s[0] ? c.length_s : "(无)");
        printf("fade=%s\n", c.fade_s[0] ? c.fade_s : "(无)");
        printf("sections=%d\n", c.n_section);
        printf("rom_size=%zu\n", c.data_size);
        printf("entry=0x%08x\n", c.entry);
        printf("length_ms=%ld\n", length_ms);
        printf("fade_ms=%ld\n", fade_ms);
        printf("native_rate=%d\n", NATIVE_RATE);
        goto done;
    }

    /* ---- 4. 算总帧数 ---- */
    if (length_ms < 0) {
        length_ms = (long)(DEFAULT_SECS * 1000.0);
        fprintf(stderr, "NOTE 没有可用的 length 标签，按默认 %.0f 秒渲染\n",
                DEFAULT_SECS);
    }
    if (want_secs > 0.0) {
        long ms = (long)(want_secs * 1000.0);
        if (ms < length_ms) length_ms = ms;
    }
    if (no_fade) fade_ms = 0;

    /* 帧数 = 毫秒 × 采样率 / 1000，全程整数，和另外三个解码器同一套口径。
       ⚠ 总长是 **length + fade**。 */
    {
        long play_ms = length_ms + fade_ms;
        total_frames = (int64_t)play_ms * (int64_t)out_rate / 1000;
    }

    {
        int64_t fade_frames = (int64_t)fade_ms * (int64_t)out_rate / 1000;
        int64_t fade_start = total_frames - fade_frames;

        out = fopen(out_path, "wb");
        if (!out) {
            fprintf(stderr, "ERR 无法写入 %s\n", out_path);
            rc = EXIT_USAGE;
            goto done;
        }
        if (write_wav_header(out, 0, out_rate, OUT_CH, OUT_BITS) != 0) {
            fprintf(stderr, "ERR 写 WAV 头失败\n");
            rc = EXIT_USAGE;
            goto done;
        }

        /* ---- 5. 渲染 ---- */
        frames_left = total_frames;
        while (frames_left > 0) {
            unsigned long n = (frames_left > BUF_FRAMES)
                              ? BUF_FRAMES : (unsigned long)frames_left;
            int j;
            int samples_into_fade;

            gsf_render(core, st, buf, n);

            if (fade_frames > 0) {
                samples_into_fade = (int)((int64_t)(total_frames - frames_left)
                                          - fade_start);
                for (j = 0; j < (int)n; j++, samples_into_fade++) {
                    if (samples_into_fade > 0) {
                        /* ⚠ 必须用 double 乘，不能用整数除法。
                           2800 × (119700/441000) 在整数下是 760，
                           浮点下是 759.999… → 截断成 759，差 1 LSB。 */
                        double fadedness = (double)(fade_frames - samples_into_fade)
                                           / (double)fade_frames;
                        buf[j * 2 + 0] = (int16_t)(buf[j * 2 + 0] * fadedness);
                        buf[j * 2 + 1] = (int16_t)(buf[j * 2 + 1] * fadedness);
                    }
                }
            }

            if (fwrite(buf, (size_t)OUT_CH * 2, (size_t)n, out) != (size_t)n) {
                fprintf(stderr, "ERR 写 WAV 数据失败\n");
                break;
            }
            data_bytes += (uint32_t)(n * OUT_CH * 2);
            frames_left -= (int64_t)n;
        }

        /* ---- 6. 回填两个长度字段（偏移 4 和 40） ---- */
        {
            uint8_t h[4];
            if (fseek(out, 4, SEEK_SET) == 0) {
                wr32le(h, 36 + data_bytes);
                fwrite(h, 1, 4, out);
            }
            if (fseek(out, 40, SEEK_SET) == 0) {
                wr32le(h, data_bytes);
                fwrite(h, 1, 4, out);
            }
        }

        if (data_bytes == 0) {
            fprintf(stderr, "ERR 渲染出 0 个样本\n");
            rc = EXIT_EMPTY;
            goto done;
        }
    }

    if (getenv("GSF2WAV_VERBOSE"))
        fprintf(stderr, "OK GBA %d 段 %lld 帧 %.2f 秒 @%u\n",
                c.n_section,
                (long long)(data_bytes / (OUT_CH * 2)),
                (double)(data_bytes / (OUT_CH * 2)) / out_rate,
                out_rate);

done:
    if (out) fclose(out);
    /* ⚠ **不要**在这里再 `rom->close(rom)`。
       `core->deinit()` → `GBADestroy()` → `GBAUnloadROM()` 里已经
       `gba->romVf->close(gba->romVf)` 了，再关一次就是二次释放
       （`_vfmClose` 会 `free(vfm)`），退出时直接堆损坏 —— 表现是
       「WAV 写得好好的，退出码却是 127」。上游 `xsf_close()` 也只
       `core->deinit()`，从没关过这个 VFile。 */
    if (core) core->deinit(core);
    if (st) free(st);
    free(c.data);
    return rc;
}
