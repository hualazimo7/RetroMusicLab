/* usf2wav.c — USF / miniusf（N64 芯片音源）→ WAV 的命令行解码器
 *
 * 为什么需要它
 * ------------
 * `.miniusf` 只有 148~489 字节，是**状态快照**：真正的音乐在配套的 `.usflib`
 * （几 MB）里，而且存的是「N64 声音驱动 + 采样库」。要出声必须真的模拟
 * R4300 CPU 和 RSP 的音频微码。这不是「写个解析器」能解决的。
 *
 * 本工具把三件事拼起来：
 *
 *   psflib    （MIT）        解析 XSF 容器：zlib 解压、`_lib` 链、tags
 *   lazyusf2  （GPLv2+）     模拟 R4300 + RSP（音频走 HLE 微码）
 *   zlib      （zlib 许可）  psflib 只用它一个 uncompress()
 *
 * 为什么**不要** BIOS
 * -------------------
 * N64 没有 BIOS 这种东西，核里自带 PIF/CIC 的 HLE 引导。所以和 psf2wav 不同，
 * 本工具**不需要任何外部文件**，拿来就能跑。
 *
 * 为什么关掉 dynarec
 * ------------------
 * lazyusf2 有 x86 / x86_64 两套 JIT。JIT 会按**编译机**的 CPU 出码，换到不支持
 * 的机器上就是 `0xC000001D`（本项目在 psf2wav 的 `-mcpu` 上已经栽过一次，见
 * README ⚠51）。这里用 `r4300/cached_interp.c` 的 cached interpreter：
 * 纯 C、零 JIT、任何 x86_64 都能跑。代价是慢一些（实测约 9 倍实时，够用）。
 *
 * `length` / `fade` 标签
 * ----------------------
 * USF 的时长**只来自标签**，不像流式格式能算出总长。没有 `length` 标签的文件
 * 如果不兜底，输出就是 0 字节（lazyusf2 自带的 `test/dumpresampled.c` 就是
 * 这个行为）。本工具的做法：
 *
 *   * 有 `length`  → 按标签渲染，再叠 `fade` 的线性淡出
 *   * 没有 `length` → 渲染 DEFAULT_SECS 秒，并在 stderr 说明一句
 *
 * 用法
 * ----
 *   usf2wav <input.miniusf> <output.wav> [秒数] [--no-fade]
 *   usf2wav --info <input.miniusf>
 *
 * `--no-fade` 关掉淡出。**逐样本比对时必须加**：淡出是乘上去的斜坡，
 * 参考实现如果没做淡出，差的就是那条曲线而不是解码结果。
 *
 * 退出码
 * ------
 *   0  成功
 *   1  参数错误
 *   2  psf_load 失败（不是有效的 USF，或者 _lib 找不到）
 *   3  渲染出 0 个样本
 *   4  内存分配失败
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usf/usf.h"

#include <psflib.h>

#define OUT_RATE    44100
#define OUT_CH      2
#define OUT_BITS    16
#define BUF_FRAMES  1024        /* 每次渲染的帧数（1024 帧 = 4096 字节） */
#define DEFAULT_SECS 180.0      /* 没有 length 标签时的兜底时长 */

enum {
    EXIT_OK = 0,
    EXIT_USAGE = 1,
    EXIT_PARSE = 2,
    EXIT_EMPTY = 3,
    EXIT_NOMEM = 4
};

/* ------------------------------------------------------------------ */
/*  小工具                                                             */
/* ------------------------------------------------------------------ */

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

/*: `1:23:45.678` / `83.5` / `1:23` → 毫秒。非法返回 -1。
 *
 * 抄自 lazyusf2 的 `test/dumpresampled.c`，但把它的 `BORK_TIME` 哨兵换成 -1，
 * 免得一个坏标签把时长算成天文数字。 */
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
/*  psflib 回调                                                         */
/* ------------------------------------------------------------------ */

typedef struct {
    char title[256];
    char game[256];
    char length_s[64];
    char fade_s[64];
    int  enable_compare;
    int  enable_fifo_full;
    int  n_section;         /* 一共喂了几个 _lib 段（排错用） */
} loadctx;

/*: 模拟器状态。**必须是全局的** —— psflib 的 load_cb 只给 `void *ctx`，
 *  而 ctx 已经用来放标签了。这个工具一次只解一个文件，单进程单状态够用。 */
static void *g_state = NULL;

/*: 每个 `_lib` 段/主文件都会被回调一次。USF 的数据在 **reserved** 段里
 *  （不是 exe 段 —— 这是 USF 和 PSF 最大的区别，照 `dumpresampled.c` 抄的）。 */
static int load_cb(void *ctx, const uint8_t *exe, size_t exe_size,
                   const uint8_t *reserved, size_t reserved_size)
{
    loadctx *c = (loadctx *)ctx;

    /* USF 不该有 exe 段；真有也忽略，不要因为一个空段整体失败 */
    (void)exe;
    (void)exe_size;

    if (!reserved || !reserved_size) return 0;

    if (usf_upload_section(g_state, reserved, reserved_size) != 0) {
        fprintf(stderr, "ERR usf_upload_section 失败（段 %d，%zu 字节）\n",
                c->n_section, reserved_size);
        return -1;
    }
    c->n_section++;
    return 0;
}

static int info_cb(void *ctx, const char *name, const char *value)
{
    loadctx *c = (loadctx *)ctx;
    if (!name || !value) return 0;
    if (!strcmp(name, "title"))  snprintf(c->title,    sizeof(c->title),    "%s", value);
    if (!strcmp(name, "game"))   snprintf(c->game,     sizeof(c->game),     "%s", value);
    if (!strcmp(name, "length")) snprintf(c->length_s, sizeof(c->length_s), "%s", value);
    if (!strcmp(name, "fade"))   snprintf(c->fade_s,   sizeof(c->fade_s),   "%s", value);
    if (!strcmp(name, "_enablecompare")  && *value) c->enable_compare  = 1;
    if (!strcmp(name, "_enablefifofull") && *value) c->enable_fifo_full = 1;
    return 0;
}

/* ---- psflib 的文件回调（Windows 上直接用 stdio） ---- */

static void *f_open(void *ctx, const char *path)
{
    (void)ctx;
    return (void *)fopen(path, "rb");
}
static size_t f_read(void *buf, size_t size, size_t count, void *h)
{
    return fread(buf, size, count, (FILE *)h);
}
static int f_seek(void *h, int64_t ofs, int whence)
{
    return fseek((FILE *)h, (long)ofs, whence);
}
static int f_close(void *h)
{
    return fclose((FILE *)h);
}
static long f_tell(void *h)
{
    return ftell((FILE *)h);
}

static const psf_file_callbacks g_callbacks = {
    "\\/",          /* path_separators */
    NULL,           /* context */
    f_open, f_read, f_seek, f_close, f_tell
};

/* ------------------------------------------------------------------ */
/*  主流程                                                             */
/* ------------------------------------------------------------------ */

static void usage(void)
{
    fprintf(stderr,
        "用法:\n"
        "  usf2wav <input.miniusf> <output.wav> [秒数] [--no-fade]\n"
        "  usf2wav --info <input.miniusf>\n"
        "\n"
        "  --no-fade   不叠淡出（逐样本比对时必须加）\n"
        "  --info      只打印标签与段数，不渲染\n");
}

int main(int argc, char **argv)
{
    const char *in_path = NULL;
    const char *out_path = NULL;
    double want_secs = 0.0;
    int info_only = 0;
    int no_fade = 0;
    int i;

    loadctx c;
    int rval;
    FILE *out = NULL;
    int16_t buf[BUF_FRAMES * OUT_CH];
    int64_t frames_left, total_frames = 0;
    uint32_t data_bytes = 0;
    long length_ms, fade_ms;

    /* ---- 参数 ---- */
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--info")) {
            info_only = 1;
        } else if (!strcmp(argv[i], "--no-fade")) {
            no_fade = 1;
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

    /* ---- 1. 分配模拟器状态 ---- */
    {
        size_t ss = usf_get_state_size();
        if (!ss) {
            fprintf(stderr, "ERR usf_get_state_size 返回 0\n");
            return EXIT_NOMEM;
        }
        g_state = calloc(1, ss);
        if (!g_state) {
            fprintf(stderr, "ERR 分配模拟器状态失败（%zu 字节）\n", ss);
            return EXIT_NOMEM;
        }
        usf_clear(g_state);
    }

    /* ---- 2. 解析 USF 容器（版本字节 0x21） ---- */
    rval = psf_load(in_path, &g_callbacks, 0x21, load_cb, &c,
                    info_cb, &c, 1, NULL, NULL);
    if (rval <= 0) {
        fprintf(stderr,
                "ERR psf_load 失败 (%d)：不是有效的 USF，"
                "或者配套的 .usflib 找不到\n", rval);
        usf_shutdown(g_state);
        free(g_state);
        return EXIT_PARSE;
    }
    if (!c.n_section) {
        fprintf(stderr, "ERR USF 里没有任何数据段\n");
        usf_shutdown(g_state);
        free(g_state);
        return EXIT_PARSE;
    }

    usf_set_compare(g_state, c.enable_compare);
    usf_set_fifo_full(g_state, c.enable_fifo_full);
    usf_set_hle_audio(g_state, 1);      /* RSP 音频走 HLE 微码 */

    length_ms = parse_time_ms(c.length_s);
    fade_ms   = parse_time_ms(c.fade_s);
    if (fade_ms < 0) fade_ms = 0;

    if (info_only) {
        printf("version=0x21\n");
        printf("title=%s\n", c.title);
        printf("game=%s\n", c.game);
        printf("length=%s\n", c.length_s[0] ? c.length_s : "(无)");
        printf("fade=%s\n", c.fade_s[0] ? c.fade_s : "(无)");
        printf("sections=%d\n", c.n_section);
        printf("length_ms=%ld\n", length_ms);
        printf("fade_ms=%ld\n", fade_ms);
        usf_shutdown(g_state);
        free(g_state);
        return EXIT_OK;
    }

    /* ---- 3. 算总帧数 ---- */
    if (length_ms < 0) {
        /* 没有 length 标签（或者标签是坏的）→ 兜底，别输出 0 字节 */
        length_ms = (long)(DEFAULT_SECS * 1000.0);
        fprintf(stderr,
                "NOTE 没有可用的 length 标签，按默认 %.0f 秒渲染\n",
                DEFAULT_SECS);
    }
    if (want_secs > 0.0) {
        long ms = (long)(want_secs * 1000.0);
        if (ms < length_ms) length_ms = ms;
    }
    if (no_fade) fade_ms = 0;

    /* 帧数换算用整数 `ms * 441 / 10`，和上游 `dumpresampled.c` **完全一致**。
       换成 `ms * 44100 / 1000.0` 这种浮点写法，理论上会在个别时长上差 1 帧，
       逐样本比对就永远对不上了。

       ⚠⚠ 总长是 **length + fade**，不是 length。
       上游 dumpresampled 的做法是「先渲染 length 帧（原样），再**追加**
       fade 帧并在这一段上乘淡出斜坡」。第一版我理解成了「只渲染 length 帧、
       把淡出压在 length 的尾巴里」，于是凡是带 fade 标签的文件都比参照
       短了整整一个 fade 时长（Banjo 那几首差 10 秒 = 1764000 字节）。
       逐样本比对一下就抓出来了 —— 这就是坚持要独立参照的价值。 */
    {
        long play_ms = length_ms + fade_ms;
        total_frames = (int64_t)play_ms * 441 / 10;
    }
    {
        int64_t fade_frames = (int64_t)fade_ms * 441 / 10;
        if (fade_frames > total_frames) fade_frames = 0;   /* 标签不合理就别淡 */

        /* ---- 4. 开文件，先占位头 ---- */
        out = fopen(out_path, "wb");
        if (!out) {
            fprintf(stderr, "ERR 无法写入 %s\n", out_path);
            usf_shutdown(g_state);
            free(g_state);
            return EXIT_USAGE;
        }
        if (write_wav_header(out, 0, OUT_RATE, OUT_CH, OUT_BITS) != 0) {
            fprintf(stderr, "ERR 写 WAV 头失败\n");
            fclose(out);
            usf_shutdown(g_state);
            free(g_state);
            return EXIT_USAGE;
        }

        /* ---- 5. 渲染 ---- */
        frames_left = total_frames;
        while (frames_left > 0) {
            int32_t n = (frames_left > BUF_FRAMES)
                        ? BUF_FRAMES : (int32_t)frames_left;
            int32_t k;

            if (usf_render_resampled(g_state, buf, n, OUT_RATE) != NULL) {
                /* 渲染器报错（一般是微码没认出来）—— 已经出的样本照样保留 */
                fprintf(stderr, "WARN usf_render 报错，提前结束\n");
                break;
            }

            /* 淡出：和 dumpresampled.c 同一套斜坡，只是按绝对位置算，
               免得跨缓冲区的时候斜率不连续 */
            if (fade_frames > 0) {
                int64_t pos = total_frames - frames_left;    /* 已出帧数 */
                int64_t fade_start = total_frames - fade_frames;
                for (k = 0; k < n; k++) {
                    int64_t p = pos + k;
                    if (p >= fade_start) {
                        int64_t left = total_frames - p;
                        buf[k * 2 + 0] = (int16_t)((int64_t)buf[k * 2 + 0]
                                                   * left / fade_frames);
                        buf[k * 2 + 1] = (int16_t)((int64_t)buf[k * 2 + 1]
                                                   * left / fade_frames);
                    }
                }
            }

            if (fwrite(buf, (size_t)OUT_CH * 2, (size_t)n, out) != (size_t)n) {
                fprintf(stderr, "ERR 写 WAV 数据失败\n");
                break;
            }
            data_bytes += (uint32_t)(n * OUT_CH * 2);
            frames_left -= n;
        }

        /* ---- 6. 回填两个长度字段 ----
         * ⚠ 偏移 4（RIFF 块长度）和偏移 40（data 块长度）。
         *   第一版把这里写成 fseek(0) 了，直接把 "RIFF" 四个字节盖掉，
         *   Python 的 wave 模块立刻报「file does not start with RIFF id」。 */
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
        fclose(out);
        out = NULL;

        if (data_bytes == 0) {
            fprintf(stderr, "ERR 渲染出 0 个样本\n");
            usf_shutdown(g_state);
            free(g_state);
            return EXIT_EMPTY;
        }
    }

    if (getenv("USF2WAV_VERBOSE"))
        fprintf(stderr, "OK %d 段 %lld 帧 %.2f 秒\n",
                c.n_section, (long long)(data_bytes / (OUT_CH * 2)),
                (double)(data_bytes / (OUT_CH * 2)) / OUT_RATE);

    usf_shutdown(g_state);
    free(g_state);
    return EXIT_OK;
}
